"""Command-line entry point (`autobill`). See docs/pipeline.md#cli."""

from __future__ import annotations

import smtplib
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from autobill import __version__
from autobill.categorize import load_rules
from autobill.classify import classify_merchants, forget_unsure
from autobill.config import data_dir, load_config
from autobill.fetch.imap import PASSWORD_ENV as IMAP_PASSWORD_ENV
from autobill.fetch.imap import ImapSource, Mailbox, MailboxError, imap_password
from autobill.fetch.mime import split_forwarded
from autobill.fetch.source import DirectorySource, RawMail
from autobill.fx import FxRates
from autobill.notify import alerts
from autobill.notify.mail import PASSWORD_ENV, Mailer, smtp_password
from autobill.pipeline import process, reparse, send_pending_reports
from autobill.report.cycle import (
    CHINA,
    build_cycle_email,
    preview_cycle_html,
    record_sent,
)
from autobill.report.monthly import month_bounds, monthly_summary, render_text
from autobill.report.pdf import PdfError, find_browser, html_to_pdf
from autobill.report.statement import render_statement_html
from autobill.report.uncategorised import rules_snippet, uncategorised_merchants
from autobill.store.db import connect, load_bill
from autobill.suggest import SuggesterUnavailable, get_suggester

_sleep = time.sleep  # tests replace it

app = typer.Typer(
    help="AutoBill: summarise credit-card statement e-mails into spending reports.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"autobill {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the version and exit.",
        callback=_print_version,
        is_eager=True,
    ),
) -> None:
    """AutoBill: summarise credit-card statement e-mails into spending reports."""


def _db():
    return connect(data_dir() / "autobill.db")


@app.command("import-dir")
def import_dir(
    path: Annotated[Path, typer.Argument(help="Directory of .eml files (searched recursively).")],
    send: Annotated[
        bool, typer.Option("--send/--no-send", help="E-mail a report for each new bill.")
    ] = True,
) -> None:
    """Import every .eml file in a directory (offline; e-mails already imported are skipped)."""
    conn = _db()
    if not _import(conn, DirectorySource(path).iter_new()):
        typer.echo("目录里没有 .eml 文件")
    typer.echo(f"数据目录：{data_dir()}")
    if send:
        _send_reports(conn)
    else:
        typer.echo("按 --no-send 的要求，没有发送报表邮件。")


def _import(conn, mails) -> list:
    """Process each mail (forwarded-as-attachment ones unwrapped first); print one line per
    original e-mail and a summary. Returns the outcomes."""
    aliases = load_config().cards.card_aliases
    counts: dict[str, int] = {}
    outcomes = []
    for mail in mails:
        parts = split_forwarded(mail.data)
        for i, data in enumerate(parts):
            source = mail.source if len(parts) == 1 else f"{mail.source}#{i + 1}"
            outcome = process(conn, data_dir(), RawMail(data, source), aliases)
            outcomes.append(outcome)
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            name = source.rsplit("/", 1)[-1]
            accounts = ", ".join(b.account_id for b in outcome.bills)
            detail = accounts or outcome.error or ""
            typer.echo(f"{outcome.status:<12} {name}  {detail}")
    if counts:
        summary = "，".join(f"{status} {n}" for status, n in sorted(counts.items()))
        typer.echo("")
        typer.echo(f"共 {sum(counts.values())} 封：{summary}")
    return outcomes


def _alerts_for(outcomes, known_accounts: set[str]) -> list[alerts.Alert]:
    """What the author should hear about from this run (docs/notify.md#提醒邮件)."""
    found: list[alerts.Alert] = []
    for o in outcomes:
        if o.status == "UNRECOGNIZED":
            found.append(alerts.unrecognized(o.message_id, o.subject, o.from_addr))
        elif o.status == "FAILED":
            found.append(alerts.failed(o.message_id, o.subject, o.bank, o.error))
        # On the very first import every card is new: that is not news.
        if known_accounts:
            for bill in o.bills:
                if bill.account_id not in known_accounts:
                    found.append(alerts.new_card(bill.account_id))
    return found


def _mailbox(config) -> Mailbox:
    """The configured central mailbox, or exit saying what is missing."""
    fetcher = config.mail_fetcher
    if not fetcher.ready:
        typer.echo("还没有配置收账单的邮箱（config.yaml 的 mail_fetcher），见 docs/setup.md。")
        raise typer.Exit(1)
    if fetcher.bad_folders:
        names = "、".join(fetcher.bad_folders)
        typer.echo(f"文件夹名只能用英文和数字（{names}），请在邮箱里改名，比如 AutoBill。")
        raise typer.Exit(1)
    password = imap_password()
    if password is None:
        typer.echo(f"没有找到环境变量 {IMAP_PASSWORD_ENV}（App 专用密码或授权码）。")
        raise typer.Exit(1)
    return Mailbox(fetcher, password)


@app.command("check-mailbox")
def check_mailbox() -> None:
    """Log in to the mailbox and report what is there; changes and sends nothing."""
    config = load_config()
    ok = True
    try:
        with _mailbox(config) as box:
            typer.echo(f"✓ 收信邮箱登录成功：{config.mail_fetcher.username}")
            wanted = config.mail_fetcher.folders
            found, missing = box.resolve(wanted)
            for folder in found:
                _, count = box.examine(folder)
                typer.echo(f"✓ 文件夹 {folder}：{count} 封邮件")
            existing = "、".join(box.folders())
            for folder in missing:
                typer.echo(f"- 邮箱里还没有文件夹 {folder}，先跳过（现有：{existing}）")
            if not found:
                ok = False
                typer.echo("✗ 配置的文件夹一个都没找到。检查 config.yaml 的 mail_fetcher.folders。")
    except MailboxError as exc:
        typer.echo(f"✗ 收信邮箱：{exc}")
        typer.echo("  检查 imap_server、username 和 App 专用密码（改过 Apple ID 密码要重新生成）")
        ok = False
    smtp = config.notifier.smtp_report
    password = smtp_password()
    if not smtp.ready:
        typer.echo("- 没有配置报表发信（notifier.smtp_report），跳过发信检查。")
    elif password is None:
        typer.echo(f"✗ 发信：没有找到环境变量 {PASSWORD_ENV} 或 {IMAP_PASSWORD_ENV}。")
        ok = False
    else:
        try:
            Mailer(smtp, password).check_login()
            typer.echo(f"✓ 发信邮箱登录成功：{smtp.username}（没有发送任何邮件）")
        except (smtplib.SMTPException, OSError) as exc:
            typer.echo(f"✗ 发信：{type(exc).__name__}: {exc}")
            ok = False
    if not ok:
        raise typer.Exit(1)
    typer.echo("全部正常。")


@app.command()
def run(
    send: Annotated[
        bool, typer.Option("--send/--no-send", help="E-mail the progress reports.")
    ] = True,
    rescan: Annotated[
        bool,
        typer.Option(
            "--rescan", help="Read the folders from the start again (after a fix or rule change)."
        ),
    ] = False,
) -> None:
    """Fetch new statements from the mailbox, process them and send the reports."""
    code = _run_once(send, rescan)
    if code:
        raise typer.Exit(code)


@app.command()
def serve(
    interval: Annotated[int, typer.Option("--interval", min=1, help="Minutes between runs.")] = 30,
    times: Annotated[
        int, typer.Option("--times", hidden=True, help="Stop after this many runs (tests).")
    ] = 0,
) -> None:
    """Keep running: fetch, process and report every INTERVAL minutes (the Docker default)."""
    done = 0
    while True:
        started = datetime.now(CHINA).strftime("%Y-%m-%d %H:%M")
        typer.echo(f"—— {started}（北京时间）开始运行 ——")
        try:
            code = _run_once()
        except typer.Exit as exc:  # e.g. no mailbox configured yet: say so, keep waiting
            code = exc.exit_code
        except Exception:  # noqa: BLE001 - one bad run must not stop the service
            traceback.print_exc()
            code = 1
        done += 1
        if times and done >= times:
            raise typer.Exit(code)
        typer.echo(f"下次运行在 {interval} 分钟后。")
        _sleep(interval * 60)


def _run_once(send: bool = True, rescan: bool = False) -> int:
    """One fetch-process-report cycle, shared by run and serve. Returns the exit code."""
    config = load_config()
    conn = _db()
    if rescan:
        # Harmless: e-mails already processed are skipped by Message-ID; failed ones retried.
        conn.execute("DELETE FROM folder_cursors")
        typer.echo("从头重读文件夹：处理过的邮件会跳过，之前失败或不认识的会重新处理。")
    known = {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM bills")}
    try:
        with _mailbox(config) as box:
            alerts.clear(conn, "mailbox", "login")  # logged in: an old login alert is over
            source = ImapSource(box, conn)
            outcomes = _import(conn, source.iter_new())
            total = len(outcomes)
            alerts.record(conn, _alerts_for(outcomes, known))
            if source.stats.missing_folders:
                names = "、".join(source.stats.missing_folders)
                typer.echo(f"邮箱里还没有这些文件夹，已跳过：{names}")
            skipped = "，".join(f"{why} {n} 封" for why, n in source.stats.skipped.items())
            line = f"邮箱里的新邮件 {source.stats.seen} 封，处理了 {total} 封"
            typer.echo(line + (f"；跳过：{skipped}" if skipped else ""))
    except MailboxError as exc:
        typer.echo(f"收信失败：{exc}")
        alerts.record(conn, [alerts.mailbox(str(exc))])
        if send:
            _send_alerts(conn)  # may fail too when both use the same password
        return 1
    _auto_classify(conn, config)
    if not send:
        typer.echo("按 --no-send 的要求，没有发送报表和提醒邮件。")
        return 0
    try:
        _send_reports(conn)
    except typer.Exit as exc:  # a report could not be sent; it is retried next run
        _send_alerts(conn)
        return exc.exit_code
    _send_alerts(conn)
    return 0


def _auto_classify(conn, config) -> None:
    """Let the AI classify new merchants before the reports are built (ai.auto_classify).
    A failure is alerted once and never stops the run: those merchants stay 未分类."""
    if not (config.ai.provider and config.ai.auto_classify):
        return
    try:
        suggester = get_suggester(config.ai)
    except SuggesterUnavailable as exc:
        typer.echo(f"AI 分类没有完成：{exc}")
        alerts.record(conn, [alerts.ai(str(exc))])
        return
    result = classify_merchants(conn, suggester, load_rules(conn), config.ai)
    if result.verdicts:
        line = f"AI 分类：问了 {len(result.verdicts)} 个新商户，分好 {len(result.used)} 个"
        typer.echo(line + (f"，还有 {result.left} 个下次再问" if result.left else ""))
    if result.error is not None:
        typer.echo(f"AI 分类没有完成：{result.error}")
        alerts.record(conn, [alerts.ai(str(result.error))])
    else:
        alerts.clear(conn, "ai", "error")


def _send_alerts(conn) -> None:
    """E-mail pending alerts, if a mailbox is configured; failures are reported, not raised."""
    config = load_config()
    smtp = config.notifier.smtp_report
    password = smtp_password()
    waiting = len(alerts.pending(conn))
    if not waiting:
        return
    if not smtp.ready or password is None:
        typer.echo(f"有 {waiting} 条提醒，但没有配置发信，这次没发出去。")
        return
    try:
        n = alerts.send_pending(conn, Mailer(smtp, password))
        typer.echo(f"已发送提醒邮件（{n} 条提醒），收件人 {smtp.to_addr}。")
    except (smtplib.SMTPException, OSError) as exc:
        typer.echo(f"提醒邮件发送失败：{type(exc).__name__}: {exc}。下次运行会再发。")


def _mailer(config) -> Mailer | None:
    """The mailer for reports, or None with the reason printed."""
    smtp = config.notifier.smtp_report
    if not smtp.ready:
        typer.echo("没有配置报表邮箱（config.yaml 的 notifier.smtp_report），这次不发送报表。")
        return None
    password = smtp_password()
    if password is None:
        missing = f"{PASSWORD_ENV} 或 {IMAP_PASSWORD_ENV}"
        typer.echo(f"没有找到环境变量 {missing}（邮箱密码），这次不发送报表。")
        return None
    return Mailer(smtp, password)


def _send_reports(conn) -> None:
    """E-mail every statement month that is complete and not reported yet."""
    config = load_config()
    mailer = _mailer(config)
    if mailer is None:
        return
    result = send_pending_reports(
        conn,
        mailer,
        FxRates(conn, config.fx),
        load_rules(conn),
        portfolio=config.cards.portfolio,
        backup=config.backup,
    )
    sent = f"已发送报表邮件 {result.emails} 封（新账单 {len(result.sent)} 份）"
    typer.echo(f"{sent}，收件人 {mailer.config.to_addr}。")
    for cycle, error in result.backup_errors:
        typer.echo(f"数据库备份失败（{cycle} 账单月）：{error}。报表照常发出，下个月会再备份。")
    if result.failed:
        cycle, error = result.failed
        typer.echo(f"发送失败（{cycle} 账单月）：{error}。没发出去的下次运行会再发。")
        raise typer.Exit(1)


@app.command()
def resend(
    cycle: Annotated[str, typer.Option("--cycle", help="Statement month, e.g. 2026-08.")],
) -> None:
    """Send a statement month's e-mail again, rebuilt from the data as it stands now.

    For after a fix: `reparse` corrected the amounts, or the AI classified merchants that
    were 未分类 when the month's e-mail went out. The new e-mail joins the same
    conversation; which statements count as reported does not change.
    """
    conn = _db()
    try:
        month_bounds(cycle)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    if not conn.execute(
        "SELECT 1 FROM bills WHERE substr(statement_date, 1, 7) = ?", (cycle,)
    ).fetchone():
        raise typer.BadParameter(f"{cycle} 没有出账的账单。")
    config = load_config()
    mailer = _mailer(config)
    if mailer is None:
        raise typer.Exit(1)
    message, report = build_cycle_email(
        conn,
        cycle,
        FxRates(conn, config.fx),
        mailer.config.username,
        mailer.config.to_addr,
        portfolio=config.cards.portfolio,
        rules=load_rules(conn),
    )
    mailer.send(message)
    record_sent(conn, cycle, message["Message-ID"], report.complete)
    typer.echo(f"已重发 {cycle} 账单月的邮件，收件人 {mailer.config.to_addr}。")
    typer.echo("内容按现在的账单和分类重算，并进同一个对话；账单的已发送状态没有改动。")


@app.command("preview-email")
def preview_email(
    cycle: Annotated[
        str | None,
        typer.Option("--cycle", help="Statement month, e.g. 2026-09 (default: the newest)."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="HTML file to write.")
    ] = None,
) -> None:
    """Write the next progress e-mail of a statement month as an HTML file (sends nothing)."""
    conn = _db()
    if cycle is None:
        row = conn.execute("SELECT MAX(statement_date) FROM bills").fetchone()
        if row[0] is None:
            raise typer.BadParameter("数据库里还没有账单，先运行 import-dir。")
        cycle = row[0][:7]
    try:
        month_bounds(cycle)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    config = load_config()
    has_bills = conn.execute(
        "SELECT 1 FROM bills WHERE substr(statement_date, 1, 7) = ?", (cycle,)
    ).fetchone()
    if not has_bills:
        raise typer.BadParameter(f"{cycle} 没有出账的账单。")
    target = output or data_dir() / "preview.html"
    html = preview_cycle_html(
        conn, cycle, FxRates(conn, config.fx), portfolio=config.cards.portfolio
    )
    target.write_text(html, encoding="utf-8")
    typer.echo(f"已生成 {cycle} 账单月的进度邮件预览：{target}")
    typer.echo("用浏览器打开，按 F12 切到手机尺寸，就能看到手机上的排版。")


@app.command()
def report(
    month: Annotated[str, typer.Option("--month", help="Month to summarise, e.g. 2026-08.")],
) -> None:
    """Print the spending summary of one month in the terminal."""
    try:
        month_bounds(month)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    conn = _db()
    summary = monthly_summary(conn, month, FxRates(conn, load_config().fx))
    typer.echo(render_text(summary))


@app.command("reparse")
def reparse_command(
    every: Annotated[
        bool, typer.Option("--all", help="Every stored e-mail, not only WARN / FAILED ones.")
    ] = False,
) -> None:
    """Parse stored e-mails again with the current parsers and card aliases (after a fix)."""
    conn = _db()
    aliases = load_config().cards.card_aliases
    query = "SELECT id FROM emails"
    if not every:
        query += " WHERE status IN ('WARN', 'UNVERIFIED', 'FAILED', 'UNRECOGNIZED')"
    ids = [r[0] for r in conn.execute(query + " ORDER BY id")]
    if not ids:
        typer.echo("没有需要重新解析的邮件。")
        return
    counts: dict[str, int] = {}
    for email_id in ids:
        outcome = reparse(conn, data_dir(), email_id, aliases)
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        accounts = ", ".join(f"{b.account_id} {b.statement_date}" for b in outcome.bills)
        name = outcome.source.rsplit("/", 1)[-1]
        typer.echo(f"{outcome.status:<12} {name}  {accounts or outcome.error or ''}")
    summary = "，".join(f"{status} {n}" for status, n in sorted(counts.items()))
    typer.echo("")
    typer.echo(f"重新解析了 {len(ids)} 封：{summary}。已经发过报表的账单不会再发。")


@app.command()
def uncategorised(
    cycle: Annotated[
        str | None, typer.Option("--cycle", help="Only this statement month, e.g. 2026-09.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many merchants to list.")] = 20,
) -> None:
    """List merchants no rule matches, with a snippet to paste into rules.yaml."""
    if cycle:
        try:
            month_bounds(cycle)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
    conn = _db()
    config = load_config()
    rules = load_rules(conn)  # merchants the AI has classified are not listed
    unknowns = uncategorised_merchants(conn, FxRates(conn, config.fx), rules, cycle)
    if not unknowns:
        typer.echo("没有未分类的消费。")
        return
    shown = unknowns[:limit]
    typer.echo(f"未分类的商户共 {len(unknowns)} 个，按金额列出前 {len(shown)} 个：")
    for i, u in enumerate(shown, 1):
        typer.echo(f"{i:>3}. {u.name}  {u.count} 笔  {u.amount_text}")
    typer.echo("")
    typer.echo(rules_snippet(shown))


@app.command()
def classify(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Merchants to ask about (default ai.per_run).")
    ] = None,
    retry: Annotated[
        bool, typer.Option("--retry", help="Ask again about merchants the AI was unsure of.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the answers without saving them.")
    ] = False,
) -> None:
    """Classify merchants the rules miss with the configured AI (see docs/notify.md)."""
    conn = _db()
    config = load_config()
    try:
        suggester = get_suggester(config.ai)
    except SuggesterUnavailable as exc:
        typer.echo(f"{exc}。")
        raise typer.Exit(1) from None
    if retry and not dry_run:
        typer.echo(f"重新询问之前没把握的 {forget_unsure(conn)} 个商户。")
    result = classify_merchants(
        conn, suggester, load_rules(conn), config.ai, limit=limit, save=not dry_run
    )
    if not result.verdicts and result.error is None:
        typer.echo("没有需要 AI 分类的新商户。")
    for name, v in result.verdicts.items():
        answer = v.category if name in result.used else f"不确定（猜 {v.category or '无'}）"
        how = "联网查过" if v.searched else "凭知识"
        typer.echo(f"{answer:<8} {name}  [{v.confidence}，{how}] {v.reason}")
    typer.echo("")
    summary = f"问了 {len(result.verdicts)} 个，分好 {len(result.used)} 个"
    if result.left:
        summary += f"；还有 {result.left} 个，再运行一次继续"
    typer.echo(summary + ("（--dry-run：没有保存）" if dry_run else "。"))
    usage = getattr(suggester, "usage", None)
    if usage:
        typer.echo(
            f"用量：输入 {usage['input_tokens']} tokens，输出 {usage['output_tokens']} tokens，"
            f"联网搜索 {usage['web_searches']} 次。"
        )
    if result.error is not None:
        kept = "" if dry_run else "（上面这些已经保存）"
        typer.echo(f"AI 分类中断：{result.error}{kept}")
        raise typer.Exit(1)


@app.command()
def statement(
    account: Annotated[
        str | None,
        typer.Option("--account", help="Card, e.g. ABC:0003 (default with --all: every card)."),
    ] = None,
    statement_date: Annotated[
        str | None, typer.Option("--date", help="Statement date YYYY-MM-DD (default: newest).")
    ] = None,
    every: Annotated[bool, typer.Option("--all", help="Every bill in the database.")] = False,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Folder to write into.")
    ] = None,
    pdf: Annotated[bool, typer.Option("--pdf/--no-pdf", help="Also print a PDF.")] = True,
) -> None:
    """Write standard statements (one unified HTML + PDF per bill, every transaction)."""
    if not every and not account:
        raise typer.BadParameter("请用 --account 指定一张卡，或用 --all 生成全部账单。")
    conn = _db()
    query, args = "SELECT id, account_id, statement_date FROM bills", []
    conditions = []
    if account:
        conditions.append("account_id = ?")
        args.append(account)
    if statement_date:
        conditions.append("statement_date = ?")
        args.append(statement_date)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY account_id, statement_date DESC"
    rows = conn.execute(query, args).fetchall()
    if account and not statement_date and not every:
        rows = rows[:1]  # the newest statement of that card
    if not rows:
        raise typer.BadParameter("数据库里没有符合条件的账单，先运行 import-dir。")

    config = load_config()
    target = output or Path(config.statement.output_dir or data_dir() / "statements")
    fx = FxRates(conn, config.fx)
    browser = find_browser(config.statement.pdf_browser) if pdf else None
    if pdf and browser is None:
        typer.echo("没有找到 Edge 或 Chrome，这次只生成 HTML。")
        typer.echo("可以在 config.yaml 的 statement.pdf_browser 里指定浏览器路径。")
    for row in rows:
        folder = target / row["account_id"].replace(":", "-")
        folder.mkdir(parents=True, exist_ok=True)
        html_path = folder / f"{row['statement_date']}.html"
        html_path.write_text(
            render_statement_html(load_bill(conn, row["id"]), fx, load_rules(conn)),
            encoding="utf-8",
        )
        written = [html_path.name]
        if browser is not None:
            try:
                html_to_pdf(html_path, html_path.with_suffix(".pdf"), browser)
                written.append(html_path.with_suffix(".pdf").name)
            except PdfError as exc:
                typer.echo(f"  PDF 生成失败：{exc}")
        typer.echo(
            f"{row['account_id']}  {row['statement_date']}  -> {folder.name}/{' + '.join(written)}"
        )
    typer.echo(f"\n共 {len(rows)} 份标准账单，保存在 {target}")
