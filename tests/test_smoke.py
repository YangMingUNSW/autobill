from typer.testing import CliRunner

from autobill import __version__, build_label
from autobill.cli import app


def test_cli_help_and_version():
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "AutoBill" in help_result.output

    version_result = runner.invoke(app, ["--version"])
    assert version_result.exit_code == 0
    assert __version__ in version_result.output


def test_the_version_names_the_commit_the_image_was_built_from(monkeypatch):
    """The Docker build records its commit (see the Dockerfile), so `--version` tells which
    one the server runs; a local run has no commit to name."""
    monkeypatch.setenv("AUTOBILL_REVISION", "15fe0d7e7a0b0e3a4139399458d0e06e74ef11c4")
    assert build_label() == f"{__version__} (15fe0d7)"
    result = CliRunner().invoke(app, ["--version"])
    assert result.output.strip() == f"autobill {__version__} (15fe0d7)"

    monkeypatch.delenv("AUTOBILL_REVISION")
    assert build_label() == __version__
