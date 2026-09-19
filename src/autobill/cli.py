"""Command-line entry point (`autobill`). See docs/pipeline.md#cli."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from autobill import __version__
from autobill.categorize import load_rules
from autobill.config import data_dir, load_config
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.notify.mail import PASSWORD_ENV, Mailer, smtp_password
from autobill.pipeline import process, send_pending_reports
from autobill.report.cycle import pdf_attacher, preview_cycle_html
from autobill.report.monthly import month_bounds, monthly_summary, render_text
from autobill.report.pdf import PdfError, find_browser, html_to_pdf
from autobill.report.statement import render_statement_html
from autobill.store.db import connect, load_bill

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
    source = DirectorySource(path)
    conn = _db()
    counts: dict[str, int] = {}
    for mail in source.iter_new():
        outcome = process(conn, data_dir(), mail)
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        name = mail.source.rsplit("/", 1)[-1]
        accounts = ", ".join(b.account_id for b in outcome.bills)
        detail = accounts or outcome.error or ""
        typer.echo(f"{outcome.status:<12} {name}  {detail}")
    summary = "，".join(f"{status} {n}" for status, n in sorted(counts.items()))
    typer.echo(f"\n共 {sum(counts.values())} 封：{summary or '目录里没有 .eml 文件'}")
    typer.echo(f"数据目录：{data_dir()}")
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
        typer.echo(f"没有找到环境变量 {PASSWORD_ENV}（邮箱授权码），这次不发送报表。")
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
