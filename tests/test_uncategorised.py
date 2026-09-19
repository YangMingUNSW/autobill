"""Uncategorised merchants (docs/notify.md#分类规则). The AI side is in test_classify.py."""

from pathlib import Path

import pytest
import yaml
from fakes import FakeFrankfurter
from typer.testing import CliRunner

from autobill import fx as fx_module
from autobill.categorize import load_rules
from autobill.cli import app
from autobill.config import FxConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.pipeline import process
from autobill.report.uncategorised import rules_snippet, uncategorised_merchants
from autobill.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109",
         ("AUD", "2026-08-24"): "4.6200", ("AUD", "2025-06-25"): "4.7000"}  # fmt: skip


@pytest.fixture
def db(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    for mail in DirectorySource(FIXTURES).iter_new():
        process(conn, isolated_data_dir, mail)
    return conn, FxRates(conn, FxConfig(), FakeFrankfurter(RATES))


def test_largest_uncategorised_merchants_first(db):
    conn, fx = db
    unknowns = uncategorised_merchants(conn, fx, load_rules())
    assert unknowns and all(u.count >= 1 for u in unknowns)
    totals = [u.cny for u in unknowns]
    assert totals == sorted(totals, reverse=True)
    names = {u.name for u in unknowns}
    assert "SUKIYA" not in names and "Lotus Hotpot Buffet" not in names  # caught by trade words


def test_cycle_limits_to_one_statement_month(db):
    conn, fx = db
    september = {u.name for u in uncategorised_merchants(conn, fx, load_rules(), "2026-09")}
    everything = {u.name for u in uncategorised_merchants(conn, fx, load_rules())}
    assert september < everything  # the June 2025 BOC merchants are left out


def test_snippet_lists_every_merchant_as_a_comment(db):
    conn, fx = db
    unknowns = uncategorised_merchants(conn, fx, load_rules())[:3]
    text = rules_snippet(unknowns)
    assert yaml.safe_load(text) is None  # all comments until the author moves a line
    assert all(u.name in text for u in unknowns)


runner = CliRunner()


@pytest.fixture
def cli_env(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(fx_module, "http_fetch", FakeFrankfurter(RATES))
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES)])
    return isolated_data_dir


def test_uncategorised_command(cli_env):
    result = runner.invoke(app, ["uncategorised", "--limit", "3"])
    assert result.exit_code == 0, result.output
    assert "未分类的商户共" in result.output and "前 3 个" in result.output
    assert "复制到 rules.yaml" in result.output
    assert not (cli_env / "rules.yaml").exists()  # never written by the program
