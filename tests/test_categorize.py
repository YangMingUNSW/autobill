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
        # seen on the author's 816 real purchases (2026-09-20)
        ("境外消费 MC DONALD'S SAVONA IT", "餐饮"),  # spaces and punctuation do not matter
        ("境外消费 SEVEN-ELEVEN TOKYO JP", "超市"),
        ("境外消费 7 ELEVEN 2064 SYDNEY AU", "超市"),
        ("境外消费 EVERYDAY PAY/1 WoolwortBella Vista AU", "超市"),  # cut short by the bank
        ("境外消费 1915 Lanzhou Beef NoodlMASCOT AU", "餐饮"),
        ("SQ *ALBA ITALIAN RESTAURAAUSVISA Apple Pay", "餐饮"),
        ("网上消费 支付宝，ＹＡＫＩＴＯＲＩ ＡＢＵＲＩ", "餐饮"),  # full-width letters
        ("网上消费 美团支付，美团App古茗（系马桩店）", "餐饮"),
        ("网上消费 程支付，上海携程国际旅行社有限公司", "旅行"),
        ("东方航空（航空客票）CHNCUP ApplePay", "旅行"),
        ("境外消费 TRANSPORTFORNSW TAP SYDNEY AU", "交通"),
        ("境外消费 UBR* PENDING.UBER.COM AMSTERDAM NL", "交通"),
        ("UBER *EATS HELP.UBER.COMAUSVISA Apple Pay", "餐饮"),  # not 交通
        ("境外消费 CWH NEWTOWN NSW NEWTOWN AU", "医药"),
        ("境外消费 AB APARTMENT BARCELONA BARCELONA ES", "住宿"),
        ("境外消费 TOKYO DISNEY RESORT CHIBA JP", "娱乐"),  # not 住宿
        ("QUDOS BANK ARENAAUSVISA Apple Pay", "娱乐"),
        ("跨行消费 Uniqlo Australia Pty Ltd Sydney AUS", "购物"),
        ("境外消费 QANTAS WINE AU NEW SOUTH WALAU", "烟酒"),  # not a flight
        ("网上消费 中移支付，中国移动集团", "通讯网络"),
        ("网上消费 网银在线，京东商城-Apple产品京东自营旗舰店", "网购"),
        ("支付宝-SUMITOMO MITSUI CARD COMPANY ,L", "微信/支付宝（未细分）"),  # no "SUICA" inside
        ("境外消费 Sydney Park Hotel Newtown AU", UNCATEGORISED),  # an Australian pub, not a hotel
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


def test_rules_yaml_in_data_dir_comes_before_the_built_in_rules(isolated_data_dir):
    (isolated_data_dir / "rules.yaml").write_text(
        "酒吧: [OLIVE]\n超市: [MAPLE]\n", encoding="utf-8"
    )
    rules = load_rules()
    assert rules.categorize("OLIVE GREEK TAVERNA") == "酒吧"  # the author's rule wins
    assert rules.categorize("Woolworths") == "超市"  # the built-in rules still apply
    assert rules.categories.count("超市") == 1  # listed once although both files have it


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


def test_spaces_punctuation_and_width_never_matter():
    rules = Rules.from_yaml("""餐饮: [MCDONALD, "UBER * EATS"]
超市: ["word:7-11"]
""")
    for text in [
        "MC DONALD'S",
        "MCDONALD'S-STORE 365",
        "ＭＣＤＯＮＡＬＤ",
        "UBER *EATS HELP",
        "UBEREATS",
    ]:
        assert rules.categorize(text) == "餐饮", text
    assert rules.categorize("7-11 TAIPEI") == "超市"
    assert rules.categorize("TEL 03-5711-2000") == UNCATEGORISED  # a whole word, not digits


def test_kana_and_chinese_characters_separate_words():
    rules = Rules.from_yaml("""餐饮: ["word:BAR"]
""")
    assert rules.categorize("东京BAR银座") == "餐饮"


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
    assert len(rows) == 181 and len(left) <= 30  # 37 before the 2026-09-20 rules
