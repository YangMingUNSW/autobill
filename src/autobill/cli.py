"""Command-line entry point (`autobill`). See docs/pipeline.md#cli."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from autobill import __version__
from autobill.config import data_dir, load_config
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.pipeline import process
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
