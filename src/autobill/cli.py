"""Command-line entry point (`autobill`). See docs/pipeline.md#cli."""

from __future__ import annotations

import smtplib
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from autobill import build_label
from autobill.categories.ai import classify_merchants, forget_unsure
from autobill.categories.provider import SuggesterUnavailable, get_suggester
from autobill.categories.rules import load_rules
from autobill.config import data_dir, load_config
from autobill.fetch.imap import PASSWORD_ENV as IMAP_PASSWORD_ENV
from autobill.fetch.imap import MailboxError
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.ledger import CHINA, month_bounds
from autobill.notify.mail import PASSWORD_ENV, Mailer, smtp_password
from autobill.pipeline import (
    record_parsers,
    reparse,
)
from autobill.report.cycle_mail import build_cycle_email, preview_cycle_html, record_sent
from autobill.report.monthly import monthly_summary, render_text
from autobill.report.pdf import PdfError, find_browser, html_to_pdf
from autobill.report.statement import render_statement_html
from autobill.report.uncategorised import rules_snippet, uncategorised_merchants
from autobill.report.year import build_year_report
from autobill.report.year_mail import build_year_email, render_year_html, year_plain_text
from autobill.service import (
    NotReady,
    import_mails,
    open_database,
    open_mailbox,
    report_mailer,
    run_once,
    send_reports,
)
from autobill.store.db import load_bill

_sleep = time.sleep  # tests replace it

app = typer.Typer(
    help="AutoBill: summarise credit-card statement e-mails into spending reports.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"autobill {build_label()}")
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


@app.command("import-dir")
def import_dir(
    path: Annotated[Path, typer.Argument(help="Directory of .eml files (searched recursively).")],
    send: Annotated[
        bool, typer.Option("--send/--no-send", help="Then e-mail the reports that are due.")
    ] = True,
) -> None:
    """Import every .eml file in a directory (offline; e-mails already imported are skipped)."""
    conn = open_database()
    if not import_mails(conn, DirectorySource(path).iter_new(), typer.echo):
        typer.echo("目录里没有 .eml 文件")
    typer.echo(f"数据目录：{data_dir()}")
    if send:
        if not send_reports(conn, typer.echo):
            raise typer.Exit(1)
    else:
        typer.echo("按 --no-send 的要求，没有发送报表邮件。")


@app.command("check-mailbox")
def check_mailbox() -> None:
    """Log in to the mailbox and report what is there; changes and sends nothing."""
    config = load_config()
    ok = True
    try:
        with open_mailbox(config) as box:
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
    except NotReady as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
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


def _run_once(send: bool = True, rescan: bool = False) -> int:
    """One run with its lines printed; `run` and `serve` share it (and serve's tests
    replace it)."""
    return run_once(send=send, rescan=rescan, say=typer.echo)


@app.command()
def run(
    send: Annotated[
        bool, typer.Option("--send/--no-send", help="E-mail the reports and alerts that are due.")
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
            code = _run_once()  # not configured yet: it says so and returns 1
        except Exception:  # noqa: BLE001 - one bad run must not stop the service
            traceback.print_exc()
            code = 1
        done += 1
        if times and done >= times:
            raise typer.Exit(code)
        typer.echo(f"下次运行在 {interval} 分钟后。")
        _sleep(interval * 60)


@app.command()
def resend(
    cycle: Annotated[str, typer.Option("--cycle", help="Statement month, e.g. 2026-08.")],
) -> None:
    """Send a statement month's e-mail again, rebuilt from the data as it stands now.

    For after a fix: `reparse` corrected the amounts, or the AI classified merchants that
    were 未分类 when the month's e-mail went out. The new e-mail joins the same
    conversation; which statements count as reported does not change.
    """
    conn = open_database()
    try:
        month_bounds(cycle)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    if not conn.execute(
        "SELECT 1 FROM bills WHERE substr(statement_date, 1, 7) = ?", (cycle,)
    ).fetchone():
        raise typer.BadParameter(f"{cycle} 没有出账的账单。")
    config = load_config()
    mailer = report_mailer(config, typer.echo)
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
    """Write a statement month's e-mail as an HTML file (sends nothing)."""
    conn = open_database()
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


@app.command("year-review")
def year_review(
    year: Annotated[int, typer.Option("--year", help="Year to review, e.g. 2026.")],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="HTML file to write.")
    ] = None,
    send: Annotated[
        bool, typer.Option("--send", help="E-mail it now; January's automatic one still goes out.")
    ] = False,
) -> None:
    """The year-in-review e-mail: an HTML file with -o, sent with --send, else as text here.

    It goes out by itself once a year (docs/notify.md#年度回顾); --send is for looking at
    the year so far on the phone, or for sending it again after a fix, and is not recorded.
    """
    conn = open_database()
    has_spending = conn.execute(
        "SELECT 1 FROM transactions WHERE substr(trans_date, 1, 4) = ?", (str(year),)
    ).fetchone()
    if not has_spending:
        raise typer.BadParameter(f"{year} 年没有消费记录。")
    config = load_config()
    fx, rules = FxRates(conn, config.fx), load_rules(conn)
    mailer = None
    if send:
        mailer = report_mailer(config, typer.echo)
        if mailer is None:
            raise typer.Exit(1)
        message, report = build_year_email(
            conn, year, fx, mailer.config.username, mailer.config.to_addr, rules=rules
        )
    else:
        report = build_year_report(conn, year, fx, rules)
    if output is not None:
        output.write_text(render_year_html(report), encoding="utf-8")
        typer.echo(f"已生成 {year} 年度回顾的预览：{output}")
        typer.echo("用浏览器打开，按 F12 切到手机尺寸，就能看到手机上的排版。")
    if mailer is not None:
        mailer.send(message)
        typer.echo(f"已发送 {year} 年度回顾，收件人 {mailer.config.to_addr}。")
        if conn.execute("SELECT 1 FROM year_reviews WHERE year = ?", (year,)).fetchone():
            typer.echo("按现在的数据重算后重发；自动发送的记录没有改动。")
        else:
            typer.echo("这是手动发的，不算年度回顾已经发过：第二年 1 月账单收齐后照样自动发一封。")
    if output is None and mailer is None:
        typer.echo(year_plain_text(report))


@app.command()
def report(
    month: Annotated[str, typer.Option("--month", help="Month to summarise, e.g. 2026-08.")],
) -> None:
    """Print the spending summary of one month in the terminal."""
    try:
        month_bounds(month)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    conn = open_database()
    summary = monthly_summary(conn, month, FxRates(conn, load_config().fx))
    typer.echo(render_text(summary))


@app.command("reparse")
def reparse_command(
    every: Annotated[
        bool, typer.Option("--all", help="Every stored e-mail, not only WARN / FAILED ones.")
    ] = False,
) -> None:
    """Parse stored e-mails again with the current parsers and card aliases (after a fix)."""
    conn = open_database()
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
    record_parsers(conn)
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
    conn = open_database()
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
    searches: Annotated[
        int | None,
        typer.Option(
            "--searches", min=1, max=10, help="Web searches per merchant (default ai.max_searches)."
        ),
    ] = None,
) -> None:
    """Classify merchants the rules miss with the configured AI (see docs/notify.md)."""
    conn = open_database()
    config = load_config()
    ai = config.ai if searches is None else config.ai.model_copy(update={"max_searches": searches})
    try:
        suggester = get_suggester(ai)
    except SuggesterUnavailable as exc:
        typer.echo(f"{exc}。")
        raise typer.Exit(1) from None
    if retry and not dry_run:
        typer.echo(f"重新询问之前没把握的 {forget_unsure(conn)} 个商户。")
    result = classify_merchants(
        conn, suggester, load_rules(conn), ai, limit=limit, save=not dry_run
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
    conn = open_database()
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
