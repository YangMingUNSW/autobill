"""Uncategorised merchants and the reserved AI interface (docs/notify.md#分类建议与-ai-接口)."""

from pathlib import Path

import pytest
import yaml
from fakes import FakeFrankfurter
from typer.testing import CliRunner

from autobill import fx as fx_module
from autobill import suggest
from autobill.categorize import load_rules
from autobill.cli import app
from autobill.config import AiConfig, FxConfig
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


class RecordingSuggester:
    """Stands in for an AI provider; records exactly what it was given."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def suggest(self, merchants, categories):
        self.calls.append((merchants, categories))
        return self.answer


@pytest.fixture
def fake_provider():
    recorder = RecordingSuggester({})
    suggest.register("fake", lambda config: recorder)
    yield recorder
    suggest._PROVIDERS.pop("fake", None)


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


def test_snippet_is_valid_yaml_once_moved(db):
    conn, fx = db
    unknowns = uncategorised_merchants(conn, fx, load_rules())[:3]
    text = rules_snippet(unknowns, {unknowns[0].name: "餐饮"})
    parsed = yaml.safe_load(text)
    assert parsed == {"餐饮": [unknowns[0].name]}  # the rest stay as comments to sort by hand
    assert "AI 建议" in text and all(u.name in text for u in unknowns)


def test_no_provider_configured_is_explained():
    with pytest.raises(suggest.SuggesterUnavailable, match="ai.provider"):
        suggest.get_suggester(AiConfig())
    with pytest.raises(suggest.SuggesterUnavailable, match="不认识"):
        suggest.get_suggester(AiConfig(provider="nope"))


def test_suggestions_only_for_asked_merchants_and_known_categories(fake_provider):
    fake_provider.answer = {"KELLYS": "餐饮", "INVENTED SHOP": "餐饮", "ICC SYDNEY": "会展"}
    got = suggest.suggest_categories(
        suggest.get_suggester(AiConfig(provider="fake")), ["KELLYS", "ICC SYDNEY"], ["餐饮"]
    )
    assert got == {"KELLYS": "餐饮"}


# --- the command ---------------------------------------------------------------------

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


def test_suggest_sends_only_merchant_and_category_names(cli_env, fake_provider):
    (cli_env / "config.yaml").write_text("ai:\n  provider: fake\n", encoding="utf-8")
    fake_provider.answer = {"ICC SYDNEY": "娱乐"}
    result = runner.invoke(app, ["uncategorised", "--suggest"])
    assert result.exit_code == 0, result.output
    assert "娱乐:   # AI 建议，确认后再用" in result.output
    ((merchants, categories),) = fake_provider.calls
    assert "ICC SYDNEY" in merchants and "餐饮" in categories
    # nothing but names: no amounts, dates or card numbers
    assert all(isinstance(m, str) and "¥" not in m and not m[:1].isdigit() for m in merchants)


def test_suggest_without_provider_still_lists(cli_env):
    result = runner.invoke(app, ["uncategorised", "--suggest"])
    assert result.exit_code == 0 and "还没有配置 AI" in result.output
    assert "复制到 rules.yaml" in result.output


def test_rules_file_is_never_written(cli_env, fake_provider):
    (cli_env / "config.yaml").write_text("ai:\n  provider: fake\n", encoding="utf-8")
    fake_provider.answer = {"ICC SYDNEY": "娱乐"}
    runner.invoke(app, ["uncategorised", "--suggest"])
    assert not (cli_env / "rules.yaml").exists()
