from pathlib import Path

import pytest
import yaml

from autobill.categorize import UNCATEGORISED, Rules, default_rules_text, load_rules
from autobill.model import TxnType

REPO = Path(__file__).parent.parent


def test_built_in_rules_equal_the_example_file():
    # The package ships a copy of rules.example.yaml; the two must never drift apart.
    example = yaml.safe_load((REPO / "rules.example.yaml").read_text(encoding="utf-8"))
    assert yaml.safe_load(default_rules_text()) == example


@pytest.mark.parametrize(
    ("description", "category"),
    [
        ("网上消费 财付通，深圳市腾讯计算机系统有限公司", "微信/支付宝（未细分）"),  # M6 criterion
        ("境外消费 Woolworths Online BellaVista AU", "超市"),
        ("境外消费 woolworths online", "超市"),  # case-insensitive
        ("TFNSW OPAL FARE SYDNEY AU", "交通"),
        ("Suica+ChargeJPN", "交通"),
        ("UBER * EATS PENDING AMSTERDAM", "餐饮"),
        ("PAYPAL *APPLE.COM/BILLAUS", "订阅"),
        ("支付宝-李四CHN", "微信/支付宝（未细分）"),
        ("OLIVE GREEK TAVERNA", "餐饮"),  # generic trade word, no merchant name needed
        ("境外消费 MAPLE SUPERMARKET SYDNEY AU", "超市"),
        ("SQ *SLOW LANE BREWING", "餐饮"),
        ("KPAY*CITY TOBACCO", "烟酒"),
        ("DUDULE PARIS", UNCATEGORISED),
    ],
)
def test_default_rules(description, category):
    assert load_rules().categorize(description) == category


def test_first_matching_rule_wins():
    rules = Rules.from_yaml("甲: [PAY]\n乙: [PAYPAL]\n")
    assert rules.categorize("PAYPAL *SPOTIFY") == "甲"


@pytest.mark.parametrize(
    ("txn_type", "category"),
    [(TxnType.FEE, "手续费"), (TxnType.INTEREST, "利息"), (TxnType.CASH, "取现")],
)
def test_fees_interest_and_cash_are_categorised_by_type(txn_type, category):
    assert load_rules().categorize("Woolworths", txn_type) == category


def test_rules_yaml_in_data_dir_overrides_defaults(isolated_data_dir):
    (isolated_data_dir / "rules.yaml").write_text("酒吧: [OLIVE]\n", encoding="utf-8")
    rules = load_rules()
    assert rules.categorize("OLIVE GREEK TAVERNA") == "酒吧"
    assert rules.categorize("Woolworths") == UNCATEGORISED  # defaults are replaced, not merged


def test_rules_path_from_environment(tmp_path, monkeypatch):
    path = tmp_path / "my-rules.yaml"
    path.write_text("超市: [MAPLE]\n", encoding="utf-8")
    monkeypatch.setenv("AUTOBILL_RULES", str(path))
    assert load_rules().categorize("MAPLE SUPERMARKET") == "超市"


@pytest.mark.parametrize("text", ["- just a list\n", "超市: Woolworths\n", "超市: [1, 2]\n"])
def test_malformed_rules_are_rejected(text):
    with pytest.raises(ValueError):
        Rules.from_yaml(text)


def test_word_keywords_match_whole_words_only():
    rules = Rules.from_yaml("""餐饮: ["word:BAR"]
超市: ["word:MARKET"]
网购: [AMAZON]
""")
    assert rules.categorize("LS Bar Planet") == "餐饮"
    assert rules.categorize("SQ *BAR*") == "餐饮"  # punctuation separates words
    assert rules.categorize("BARBER SHOP") == UNCATEGORISED
    assert rules.categorize("LUCKY ASIAN MARKET") == "超市"
    assert rules.categorize("AMAZON MARKETPLACE") == "网购"  # MARKET inside a longer word


def test_merchant_name_is_matched_too():
    """BOC glues the country onto the merchant ("KELLYSAUS"); the parser splits it off,
    and a whole-word rule can then match the merchant name."""
    rules = Rules.from_yaml("""餐饮: ["word:KELLYS"]
""")
    assert rules.categorize("KELLYSAUS") == UNCATEGORISED
    assert rules.categorize("KELLYSAUS", TxnType.PURCHASE, "KELLYS") == "餐饮"


def test_empty_word_keyword_is_ignored():
    rules = Rules.from_yaml("""餐饮: ["word:", "  "]
""")
    assert rules.categorize("anything") == UNCATEGORISED


def test_new_rules_cover_most_sample_purchases(isolated_data_dir):
    """The trade words must keep most real purchases out of 未分类 (86 of 181 before)."""
    from autobill.fetch.source import DirectorySource
    from autobill.pipeline import process
    from autobill.store.db import connect

    conn = connect(isolated_data_dir / "autobill.db")
    for mail in DirectorySource(Path(__file__).parent / "fixtures").iter_new():
        process(conn, isolated_data_dir, mail)
    rules = load_rules()
    rows = conn.execute(
        "SELECT description_raw, merchant FROM transactions WHERE txn_type = 'purchase'"
    ).fetchall()
    left = [r for r in rows if rules.categorize(r[0], TxnType.PURCHASE, r[1]) == UNCATEGORISED]
    assert len(rows) == 181 and len(left) <= 40
