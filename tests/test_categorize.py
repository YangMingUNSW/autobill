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
        ("OLIVE GREEK TAVERNA", UNCATEGORISED),
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
