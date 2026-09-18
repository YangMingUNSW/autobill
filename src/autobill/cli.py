"""Command-line entry point (`autobill`). See docs/pipeline.md#cli."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from autobill import __version__
from autobill.config import data_dir, load_config
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.notify.mail import PASSWORD_ENV, Mailer, smtp_password
from autobill.pipeline import process, send_pending_reports
from autobill.report.mail_report import preview_html
from autobill.report.monthly import month_bounds, monthly_summary, render_text
from autobill.store.db import connect

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
    result = send_pending_reports(conn, Mailer(smtp, password), FxRates(conn, config.fx))
    typer.echo(f"已发送报表邮件 {len(result.sent)} 封，收件人 {smtp.to_addr}。")
    if result.failed:
        bill_id, error = result.failed
        typer.echo(f"发送失败（账单 #{bill_id}）：{error}。没发出去的下次运行会再发。")
        raise typer.Exit(1)


@app.command("preview-email")
def preview_email(
    account: Annotated[
        str | None, typer.Option("--account", help="Card, e.g. ABC:0001 (default: newest bill).")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="HTML file to write.")
    ] = None,
) -> None:
    """Write the report e-mail of a bill as an HTML file to check the layout (sends nothing)."""
    conn = _db()
    query = "SELECT id, account_id, statement_date FROM bills"
    args: tuple = ()
    if account:
        query += " WHERE account_id = ?"
        args = (account,)
    row = conn.execute(query + " ORDER BY statement_date DESC, id DESC LIMIT 1", args).fetchone()
    if row is None:
        raise typer.BadParameter("数据库里没有这张卡的账单，先运行 import-dir。")
    target = output or data_dir() / "preview.html"
    html = preview_html(conn, row["id"], FxRates(conn, load_config().fx))
    target.write_text(html, encoding="utf-8")
    typer.echo(f"已生成 {row['account_id']} {row['statement_date']} 账单的报表预览：{target}")
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
