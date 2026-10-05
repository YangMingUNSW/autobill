"""ICBC parser (docs/banks/icbc.md): the author's anonymised statements of two accounts,
2025-03 to 2026-06, plus edits of them for cases they do not cover."""

import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from autobill.fetch.message import RawMessage
from autobill.model import TxnType
from autobill.parse.base import TemplateChanged
from autobill.parse.icbc import IcbcHtmlParser
from autobill.parse.registry import find_parser

FIXTURES = Path(__file__).parent / "fixtures"
ICBC = sorted((FIXTURES / "icbc").glob("*.eml"))
MULTI = "icbc_a_2025-04"  # 人民币, 港币, 美元, 澳元; Visa dining rebates; an overpayment
OLD_LAYOUT = "icbc_a_2025-03"  # 积分信息 instead of 工银i豆
THREE_CARDS = "icbc_a_2026-02"  # one account, three cards; 年费减免 0.00
MOVED_OUT = "icbc_a_2026-06"  # the USD overpayment moved out (网转)
SECOND = "icbc_b_2025-05"  # the second account; 刷卡金
CLOSING = "icbc_b_2026-03"  # 预约销户: no summary rows at all
D = Decimal
parser = IcbcHtmlParser()


def load(name: str) -> RawMessage:
    return RawMessage.from_bytes((FIXTURES / "icbc" / f"{name}.eml").read_bytes())


def parse_one(msg: RawMessage):
    (bill,) = parser.parse(msg)
    return bill


def replaced(name: str, old: str, new: str) -> RawMessage:
    msg = load(name)
    assert old in msg.html
    return dataclasses.replace(msg, html_parts=[msg.html.replace(old, new)])


def balance(bill, currency):
    (b,) = [b for b in bill.balances if b.currency == currency]
    return b


@pytest.mark.parametrize("name", [MULTI, MOVED_OUT, CLOSING])
def test_snapshot(snapshot, name):
    snapshot(f"icbc/{name}", parse_one(load(name)).model_dump(mode="json"))


@pytest.mark.parametrize("path", ICBC, ids=lambda p: p.stem)
def test_every_sample_reconciles_without_warnings(path):
    bill = parse_one(RawMessage.from_bytes(path.read_bytes()))
    assert bill.status == "OK", bill.warnings
    assert bill.warnings == []
    assert not any(t.synthetic for t in bill.transactions)


@pytest.mark.parametrize("path", ICBC, ids=lambda p: p.stem)
def test_registry_finds_icbc(path):
    assert isinstance(find_parser(RawMessage.from_bytes(path.read_bytes())), IcbcHtmlParser)


def test_matches_only_icbc():
    others = [p for p in FIXTURES.rglob("*.eml") if p.parent.name != "icbc"]
    assert others
    for path in others:
        assert not parser.matches(RawMessage.from_bytes(path.read_bytes())), path


def test_dates_and_account():
    bill = parse_one(load(THREE_CARDS))
    assert bill.bank == "ICBC"
    assert bill.account_id == "ICBC:0009"
    assert bill.cards == ["0009", "0010", "0011"]
    assert (bill.period_start, bill.period_end) == (date(2026, 1, 26), date(2026, 2, 25))
    assert bill.statement_date == date(2026, 2, 25)
    assert bill.due_date == date(2026, 3, 13)


def test_each_currency_is_an_account():
    bill = parse_one(load(MULTI))
    assert [b.currency for b in bill.balances] == ["CNY", "HKD", "USD", "AUD"]
    cny = balance(bill, "CNY")
    assert (cny.previous_balance, cny.new_charges, cny.payments_credits, cny.amount_due) == (
        D("962.43"),
        D("99.20"),
        D("962.43"),
        D("99.20"),
    )
    assert cny.min_payment == D("9.92")
    aud = balance(bill, "AUD")
    assert (aud.amount_due, aud.min_payment) == (D("1424.08"), D("142.41"))
    # Five USD rebates leave an overpayment: ICBC prints it positive, a debt negative.
    usd = balance(bill, "USD")
    assert (usd.payments_credits, usd.amount_due, usd.deposit, usd.min_payment) == (
        D("10.00"),
        0,
        D("10.00"),
        None,
    )


def test_directions_become_signs():
    bill = parse_one(load(MULTI))
    repayments = [t for t in bill.transactions if t.txn_type == TxnType.REPAYMENT]
    assert {(t.amount, t.currency, t.group_raw) for t in repayments} == {
        (D("-962.43"), "CNY", "转账"),
        (D("-97.55"), "AUD", "转帐"),
    }
    purchases = [t for t in bill.transactions if t.txn_type == TxnType.PURCHASE]
    assert purchases and all(t.amount > 0 for t in purchases)
    assert all(t.merchant == t.description_raw for t in purchases)


def test_foreign_purchase_charged_in_yuan_keeps_its_currency():
    bill = parse_one(load(MULTI))
    (t,) = [t for t in bill.transactions if t.group_raw == "跨行消费"]
    assert (t.amount, t.currency, t.orig_amount, t.orig_currency) == (
        D("99.20"),
        "CNY",
        D("21.32"),
        "AUD",
    )
    (hkd,) = [t for t in bill.transactions if t.currency == "HKD"]
    assert (hkd.amount, hkd.orig_amount, hkd.orig_currency) == (D("49.00"), None, None)


def test_rebates_are_rebates_not_refunds():
    rebates = [t for t in parse_one(load(MULTI)).transactions if t.txn_type == TxnType.REBATE]
    assert len(rebates) == 5
    assert {(t.group_raw, t.description_raw, t.amount) for t in rebates} == {
        ("境外退货", "ICBCVisaDiningRebate", D("-2.00"))
    }
    (reward,) = [t for t in parse_one(load(SECOND)).transactions if t.txn_type == TxnType.REBATE]
    assert (reward.group_raw, reward.amount) == ("刷卡金", D("-10.00"))


def test_overpayment_moved_out_is_an_adjustment():
    bill = parse_one(load(MOVED_OUT))
    (moved,) = [t for t in bill.transactions if t.currency == "USD"]
    assert (moved.group_raw, moved.txn_type, moved.amount) == (
        "网转",
        TxnType.ADJUSTMENT,
        D("10.00"),
    )
    usd = balance(bill, "USD")
    # ICBC counts it in 本期支出; here it is an adjustment, so the identity still holds.
    assert (usd.previous_deposit, usd.new_charges, usd.adjustments, usd.deposit) == (
        D("10.00"),
        0,
        D("10.00"),
        0,
    )


def test_zero_notes_are_adjustments_without_a_minus_zero():
    (waiver,) = [t for t in parse_one(load(THREE_CARDS)).transactions if t.group_raw == "年费减免"]
    assert waiver.txn_type == TxnType.ADJUSTMENT
    assert str(waiver.amount) == "0.00"


def test_closing_statement_without_summary_rows():
    bill = parse_one(load(CLOSING))
    assert bill.account_id == "ICBC:0012"
    (t,) = bill.transactions
    assert (t.group_raw, t.txn_type, t.amount) == ("预约销户", TxnType.ADJUSTMENT, 0)
    (cny,) = bill.balances
    assert (cny.currency, cny.previous_balance, cny.amount_due) == ("CNY", 0, 0)


def test_older_layout_points_section_ends_the_transactions():
    bill = parse_one(load(OLD_LAYOUT))
    assert bill.warnings == []
    assert len(bill.transactions) == 15


def test_missing_summary_raises_template_changed():
    with pytest.raises(TemplateChanged):
        parse_one(replaced(MULTI, "本 期 交 易 汇 总", "本期汇总"))


def test_missing_dates_raise_template_changed():
    msg = replaced(MULTI, "对账单生成日", "生成日")
    msg = dataclasses.replace(msg, html_parts=[msg.html.replace("账单周期", "周期")])
    with pytest.raises(TemplateChanged):
        parse_one(msg)


def test_due_amount_is_cross_checked():
    # Only the 需还款明细 row (the 合计 row is bold, the summary rows have no currency name)
    old = "澳大利亚元</td><td align=right>1,424.08/AUD"
    bill = parse_one(replaced(MULTI, old, old.replace("1,424.08", "1,424.80")))
    assert any("AUD 需还款明细的应还款额 1424.80" in w for w in bill.warnings)
    assert bill.status == "WARN"


def test_unknown_type_is_flagged_and_kept_as_spending():
    bill = parse_one(replaced(SECOND, "<td align=left>消费</td>", "<td align=left>新奇支出</td>"))
    assert any("不认识的交易类型 新奇支出" in w for w in bill.warnings)
    assert bill.status == "WARN"
    assert [t.txn_type for t in bill.transactions if t.group_raw == "新奇支出"] == [
        TxnType.PURCHASE
    ] * 3
