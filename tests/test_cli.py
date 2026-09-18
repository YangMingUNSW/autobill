"""The two M3 commands end to end, offline (the rate service is replaced)."""

from pathlib import Path

import pytest
from fakes import FakeFrankfurter
from typer.testing import CliRunner

from autobill import fx
from autobill.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    fake = FakeFrankfurter({("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109"})
    monkeypatch.setattr(fx, "http_fetch", fake)
    return fake


def test_import_dir_then_report(isolated_data_dir):
    result = runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    assert result.exit_code == 0, result.output
    assert "共 3 封：OK 3" in result.output
    assert (isolated_data_dir / "autobill.db").exists()

    again = runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    assert "共 3 封：SKIPPED 3" in again.output

    report = runner.invoke(app, ["report", "--month", "2026-08"])
    assert report.exit_code == 0, report.output
    assert "人民币合计：15,625.93" in report.output


def test_report_rejects_bad_month():
    result = runner.invoke(app, ["report", "--month", "2026-13"])
    assert result.exit_code != 0


def test_import_dir_missing_directory(tmp_path):
    result = runner.invoke(app, ["import-dir", str(tmp_path / "nope")])
    assert result.exit_code != 0
