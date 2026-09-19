"""Command-line entry point (`autobill`). See docs/pipeline.md#cli."""

from __future__ import annotations

import smtplib
from pathlib import Path
from typing import Annotated

import typer

from autobill import __version__
from autobill.categorize import load_rules
from autobill.config import data_dir, load_config
from autobill.fetch.imap import PASSWORD_ENV as IMAP_PASSWORD_ENV
from autobill.fetch.imap import ImapSource, Mailbox, MailboxError, imap_password
from autobill.fetch.mime import split_forwarded
from autobill.fetch.source import DirectorySource, RawMail
from autobill.fx import FxRates
from autobill.notify.mail import PASSWORD_ENV, Mailer, smtp_password
from autobill.pipeline import process, send_pending_reports
from autobill.report.cycle import pdf_attacher, preview_cycle_html
from autobill.report.monthly import month_bounds, monthly_summary, render_text
from autobill.report.pdf import PdfError, find_browser, html_to_pdf
from autobill.report.statement import render_statement_html
from autobill.report.uncategorised import rules_snippet, uncategorised_merchants
from autobill.store.db import connect, load_bill
from autobill.suggest import SuggesterUnavailable, get_suggester, suggest_categories

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


def _import(conn, mails) -> int:
    """Process each mail (forwarded-as-attachment ones unwrapped first); print one line per
    original e-mail and a summary. Returns how many were processed."""
    aliases = load_config().cards.card_aliases
    counts: dict[str, int] = {}
    for mail in mails:
        parts = split_forwarded(mail.data)
        for i, data in enumerate(parts):
            source = mail.source if len(parts) == 1 else f"{mail.source}#{i + 1}"
            outcome = process(conn, data_dir(), RawMail(data, source), aliases)
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            name = source.rsplit("/", 1)[-1]
            accounts = ", ".join(b.account_id for b in outcome.bills)
            detail = accounts or outcome.error or ""
            typer.echo(f"{outcome.status:<12} {name}  {detail}")
    if counts:
        summary = "，".join(f"{status} {n}" for status, n in sorted(counts.items()))
        typer.echo("")
        typer.echo(f"共 {sum(counts.values())} 封：{summary}")
    return sum(counts.values())


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
    config = load_config()
    conn = _db()
    if rescan:
        # Harmless: e-mails already processed are skipped by Message-ID; failed ones retried.
        conn.execute("DELETE FROM folder_cursors")
        typer.echo("从头重读文件夹：处理过的邮件会跳过，之前失败或不认识的会重新处理。")
    try:
        with _mailbox(config) as box:
            source = ImapSource(box, conn)
            total = _import(conn, source.iter_new())
            if source.stats.missing_folders:
                names = "、".join(source.stats.missing_folders)
                typer.echo(f"邮箱里还没有这些文件夹，已跳过：{names}")
            skipped = "，".join(f"{why} {n} 封" for why, n in source.stats.skipped.items())
            line = f"邮箱里的新邮件 {source.stats.seen} 封，处理了 {total} 封"
            typer.echo(line + (f"；跳过：{skipped}" if skipped else ""))
    except MailboxError as exc:
        typer.echo(f"收信失败：{exc}")
        raise typer.Exit(1) from None
    if send:
        _send_reports(conn)
    else:
        typer.echo("按 --no-send 的要求，没有发送报表邮件。")


def _send_reports(conn) -> None:
    """E-mail every bill that has no report yet, if a mailbox is configured."""
    config = load_config()
    smtp = config.notifier.smtp_report
    if not smtp.ready:
        typer.echo("没有配置报表邮箱（config.yaml 的 notifier.smtp_report），这次不发送报表。")
        return
    password = smtp_password()
    if password is None:
        missing = f"{PASSWORD_ENV} 或 {IMAP_PASSWORD_ENV}"
        typer.echo(f"没有找到环境变量 {missing}（邮箱密码），这次不发送报表。")
        return
    fx, rules = FxRates(conn, config.fx), load_rules()
    browser = find_browser(config.statement.pdf_browser)
    if browser is None:
        typer.echo("没有找到 Edge 或 Chrome，这次邮件不附标准账单 PDF。")
    result = send_pending_reports(
        conn,
        Mailer(smtp, password),
        fx,
        rules,
        portfolio=config.cards.portfolio,
        attach=pdf_attacher(fx, rules, browser) if browser else None,
    )
    sent = f"已发送报表邮件 {result.emails} 封（新账单 {len(result.sent)} 份）"
    typer.echo(f"{sent}，收件人 {smtp.to_addr}。")
    if result.failed:
        cycle, error = result.failed
        typer.echo(f"发送失败（{cycle} 账单月）：{error}。没发出去的下次运行会再发。")
        raise typer.Exit(1)


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


@app.command()
def uncategorised(
    cycle: Annotated[
        str | None, typer.Option("--cycle", help="Only this statement month, e.g. 2026-09.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many merchants to list.")] = 20,
    suggest: Annotated[
        bool, typer.Option("--suggest", help="Ask the configured AI for category suggestions.")
    ] = False,
) -> None:
    """List merchants no rule matches, with a snippet to paste into rules.yaml."""
    if cycle:
        try:
            month_bounds(cycle)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
    conn = _db()
    config = load_config()
    rules = load_rules()
    unknowns = uncategorised_merchants(conn, FxRates(conn, config.fx), rules, cycle)
    if not unknowns:
        typer.echo("没有未分类的消费。")
        return
    shown = unknowns[:limit]
    typer.echo(f"未分类的商户共 {len(unknowns)} 个，按金额列出前 {len(shown)} 个：")
    for i, u in enumerate(shown, 1):
        typer.echo(f"{i:>3}. {u.name}  {u.count} 笔  {u.amount_text}")
    suggestions: dict[str, str] = {}
    if suggest:
        try:
            suggester = get_suggester(config.ai)
            # Only merchant names and category names leave this computer.
            suggestions = suggest_categories(suggester, [u.name for u in shown], rules.categories)
        except SuggesterUnavailable as exc:
            typer.echo("")
            typer.echo(f"{exc}，这次不给建议。")
    typer.echo("")
    typer.echo(rules_snippet(shown, suggestions))


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
            render_statement_html(load_bill(conn, row["id"]), fx), encoding="utf-8"
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
