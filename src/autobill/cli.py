"""Command-line entry point (`autobill`). Subcommands arrive in later milestones."""

import typer

from autobill import __version__

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
