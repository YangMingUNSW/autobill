"""AI classification of merchants the rules miss (docs/notify.md#ai-分类), against a fake
model: no network."""

from dataclasses import fields
from pathlib import Path

import pytest
from fakes import FakeFrankfurter
from typer.testing import CliRunner

from autobill import fx as fx_module
from autobill import suggest
from autobill.categorize import UNCATEGORISED, load_rules
from autobill.classify import classify_merchants, forget_unsure, pending_merchants
from autobill.cli import _auto_classify, app
from autobill.config import AiConfig, Config, FxConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.notify import alerts
from autobill.pipeline import process
from autobill.report.statement import build_view
from autobill.store.db import connect, load_bill
from autobill.suggest import AnswerCutOff, BadAnswer, MerchantInfo, SuggesterError, Verdict

FIXTURES = Path(__file__).parent / "fixtures"
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109",
         ("AUD", "2026-08-24"): "4.6200", ("AUD", "2025-06-25"): "4.7000"}  # fmt: skip


class FakeModel:
    """Answers from a script: step 1 (no search) and step 2 (search) separately.
    Records exactly what it was given."""

    model = "fake-model"

    def __init__(self, known=None, found=None, fail_on_search=False):
        self.known = known or {}  # name -> Verdict without searching
        self.found = found or {}  # name -> Verdict after searching
        self.fail_on_search = fail_on_search
        self.calls: list[tuple[list[MerchantInfo], list[str], bool]] = []

    def classify(self, merchants, categories, *, search):
        self.calls.append((merchants, categories, search))
        if search and self.fail_on_search:
            raise SuggesterError("AI 接口返回 402：账户余额不足，去平台充值")
        table = self.found if search else self.known
        return {m.name: table[m.name] for m in merchants if m.name in table}


@pytest.fixture
def db(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    for mail in DirectorySource(FIXTURES).iter_new():
        process(conn, isolated_data_dir, mail)
    return conn


def names(conn, n):
    return [m.name for m in pending_merchants(conn, load_rules(conn))[:n]]


def stored(conn):
    return {r[0]: tuple(r)[1:] for r in conn.execute(
        "SELECT merchant, category, guess, confidence, searched FROM ai_categories")}  # fmt: skip


def test_no_provider_configured_is_explained():
    with pytest.raises(suggest.SuggesterUnavailable, match="ai.provider"):
        suggest.get_suggester(AiConfig())
    with pytest.raises(suggest.SuggesterUnavailable, match="不认识"):
        suggest.get_suggester(AiConfig(provider="nope"))


def test_invented_merchants_and_categories_are_dropped():
    asked = [MerchantInfo("KELLYS")]
    answer = {"KELLYS": Verdict("会展", "high"), "INVENTED": Verdict("餐饮", "high")}
    assert suggest.keep_valid(answer, asked, ["餐饮"]) == {"KELLYS": Verdict(None, "low")}


def test_the_model_learns_only_name_location_and_currency(db):
    pending = pending_merchants(db, load_rules(db))
    assert pending, "the samples have merchants no rule matches"
    assert [f.name for f in fields(MerchantInfo)] == ["name", "location", "currency"]
    for m in pending:
        assert not any(ch in (m.location or "") + (m.currency or "") for ch in "¥$")
        assert m.currency is None or (len(m.currency) == 3 and m.currency.isupper())
    categorised = {"SUKIYA", "Lotus Hotpot Buffet"}  # rules catch these: never sent
    assert not categorised & {m.name for m in pending}


def test_sure_answers_are_used_and_the_unsure_are_searched(db):
    a, b, c = names(db, 3)
    known = {a: Verdict("餐饮", "high", "拉面店"), b: Verdict("购物", "medium")}
    known[c] = Verdict(None, "low")
    found = {b: Verdict("娱乐", "medium", "网上查到是剧场", True)}
    found[c] = Verdict("超市", "low", "", True)
    model = FakeModel(known=known, found=found)
    result = classify_merchants(db, model, load_rules(db), AiConfig(), limit=3)
    assert [search for _, _, search in model.calls] == [False, True, True]  # a is not searched
    assert [ms[0].name for ms, _, search in model.calls if search] == [b, c]
    assert "微信/支付宝（未细分）" not in model.calls[0][1]  # not a category to pick
    assert result.used == {a, b}
    assert stored(db) == {
        a: ("餐饮", "餐饮", "high", 0),
        b: ("娱乐", "娱乐", "medium", 1),
        c: (None, "超市", "low", 1),  # the guess is kept, but not used
    }
    rules = load_rules(db)
    assert rules.categorize(a) == "餐饮" and rules.categorize(c) == UNCATEGORISED
    assert rules.by_ai(a) and not rules.by_ai(c)


def test_each_merchant_is_asked_once(db):
    a, b = names(db, 2)
    model = FakeModel(known={a: Verdict("餐饮", "high"), b: Verdict(None, "low")})
    classify_merchants(db, model, load_rules(db), AiConfig(), limit=2)
    again = FakeModel()
    classify_merchants(db, again, load_rules(db), AiConfig(), limit=2)
    assert all(a not in [m.name for m in ms] and b not in [m.name for m in ms]
               for ms, _, _ in again.calls)  # fmt: skip
    forget_unsure(db)  # `classify --retry`: the unsure (b among them) are asked again
    assert b in names(db, 50) and a not in names(db, 50)


def test_without_web_search_only_high_answers_are_used(db):
    (a,) = names(db, 1)
    model = FakeModel(known={a: Verdict("餐饮", "medium")})
    result = classify_merchants(db, model, load_rules(db), AiConfig(web_search=False), limit=1)
    assert [s for _, _, s in model.calls] == [False] and result.used == set()


def test_per_run_limit(db):
    total = len(pending_merchants(db, load_rules(db)))
    result = classify_merchants(db, FakeModel(), load_rules(db), AiConfig(per_run=2))
    assert len(result.verdicts) == 2 and result.left == total - 2


def test_a_rule_always_wins_over_the_ai(db, isolated_data_dir):
    (a,) = names(db, 1)
    classify_merchants(db, FakeModel(known={a: Verdict("餐饮", "high")}), load_rules(db),
                       AiConfig(), limit=1)  # fmt: skip
    (isolated_data_dir / "rules.yaml").write_text(f'娱乐: ["{a}"]\n', encoding="utf-8")
    rules = load_rules(db)
    assert rules.categorize(a) == "娱乐" and not rules.by_ai(a)


def test_failure_part_way_keeps_the_answers_before_it(db):
    a, b = names(db, 2)
    model = FakeModel(known={a: Verdict("餐饮", "high")}, fail_on_search=True)
    result = classify_merchants(db, model, load_rules(db), AiConfig(), limit=2)
    assert "余额不足" in str(result.error) and result.left >= 1
    assert set(stored(db)) == {a}  # b was never answered: asked again next time


class CutsOff(FakeModel):
    """Like DeepSeek thinking at length: more than 3 merchants do not fit one answer."""

    def classify(self, merchants, categories, *, search):
        if len(merchants) > 3:
            self.calls.append((merchants, categories, search))
            raise AnswerCutOff("AI 的回答太长被截断")
        return super().classify(merchants, categories, search=search)


def test_a_bad_answer_about_one_merchant_does_not_stop_the_rest(db):
    a, b, c = names(db, 3)

    class Flaky(FakeModel):
        def classify(self, merchants, categories, *, search):
            if search and merchants[0].name == b:
                self.calls.append((merchants, categories, search))
                raise BadAnswer("AI 的回答里没有要求的 JSON 结果")
            return super().classify(merchants, categories, search=search)

    found = {c: Verdict("餐饮", "medium", "", True)}
    model = Flaky(known={a: Verdict("餐饮", "high")}, found=found)
    result = classify_merchants(db, model, load_rules(db), AiConfig(), limit=3)
    assert result.error is None and result.used == {a, c}
    assert stored(db)[b][0] is None  # b stays unsure; `classify --retry` asks again


def test_a_batch_whose_answer_is_cut_off_is_asked_in_halves(db):
    ten = names(db, 10)
    model = CutsOff(known={n: Verdict("餐饮", "high") for n in ten})
    result = classify_merchants(db, model, load_rules(db), AiConfig(), limit=10)
    assert result.error is None and result.used == set(ten)
    assert [len(ms) for ms, _, _ in model.calls] == [10, 5, 2, 3, 5, 2, 3]


def test_transaction_list_marks_ai_categories(db):
    (name,) = names(db, 1)
    (bill_id,) = db.execute(
        "SELECT bill_id FROM transactions WHERE merchant = ?"
        " OR (merchant IS NULL AND description_raw = ?)",
        (name, name),
    ).fetchone()
    db.execute(
        "INSERT INTO ai_categories VALUES (?, ?, ?, 'high', '', 0, NULL, NULL, 'm', 'now')",
        (name, "健身", "健身"),
    )
    fx = FxRates(db, FxConfig(), FakeFrankfurter(RATES))
    view = build_view(load_bill(db, bill_id), fx, load_rules(db))
    lines = [t for day in view.days for t in day.lines if t.title == name]
    assert lines and lines[0].meta[0] == "健身（AI）"
    assert view.categories.get("健身")  # and it counts in the category totals


# --- run / serve and the command ------------------------------------------------------


def test_auto_classify_failure_is_alerted_once_and_cleared(db, monkeypatch):
    config = Config(ai=AiConfig(provider="fake"))
    model = FakeModel(fail_on_search=True)
    suggest.register("fake", lambda c: model)
    try:
        _auto_classify(db, config)  # step 1 answers nothing -> search -> 402
        assert [a.kind for _, a in alerts.pending(db)] == ["ai"]
        _auto_classify(db, config)
        assert len(alerts.pending(db)) == 1  # the same problem is not repeated
        model.fail_on_search = False
        _auto_classify(db, config)
        assert alerts.pending(db) == []  # working again: the alert is cleared
    finally:
        suggest._PROVIDERS.pop("fake", None)


def test_auto_classify_is_off_without_a_provider(db):
    _auto_classify(db, Config())
    assert stored(db) == {}


runner = CliRunner()


@pytest.fixture
def cli_env(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(fx_module, "http_fetch", FakeFrankfurter(RATES))
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES)])
    (isolated_data_dir / "config.yaml").write_text("ai:\n  provider: fake\n", encoding="utf-8")
    conn = connect(isolated_data_dir / "autobill.db")
    first = names(conn, 1)[0]
    model = FakeModel(known={first: Verdict("餐饮", "high", "拉面店")})
    suggest.register("fake", lambda c: model)
    yield conn, first
    suggest._PROVIDERS.pop("fake", None)


def test_classify_command(cli_env):
    conn, first = cli_env
    result = runner.invoke(app, ["classify", "--dry-run", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert f"餐饮       {first}" in result.output and "拉面店" in result.output
    assert "没有保存" in result.output and stored(conn) == {}
    result = runner.invoke(app, ["classify", "--limit", "2"])
    assert "分好 1 个" in result.output and stored(conn)[first][0] == "餐饮"
    listed = runner.invoke(app, ["uncategorised", "--limit", "500"]).output
    assert first not in listed  # classified now


def test_classify_without_provider(isolated_data_dir):
    result = runner.invoke(app, ["classify"])
    assert result.exit_code == 1 and "还没有配置 AI" in result.output
