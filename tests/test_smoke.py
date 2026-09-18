from typer.testing import CliRunner

from autobill import __version__
from autobill.cli import app


def test_cli_help_and_version():
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "AutoBill" in help_result.output

    version_result = runner.invoke(app, ["--version"])
    assert version_result.exit_code == 0
    assert __version__ in version_result.output
