"""ABC parser against the three anonymised real statements (docs/banks/abc.md)."""

import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from autobill.fetch.message import RawMessage
from autobill.model import TxnType
from autobill.parse.abc import AbcHtmlParser
from autobill.parse.base import TemplateChanged
from autobill.reconcile import SYNTHETIC_DESCRIPTION

FIXTURES = Path(__file__).parent / "fixtures"
ABC = sorted((FIXTURES / "abc").glob("*.eml"))
D = Decimal
parser = AbcHtmlParser()


def load(name: str) -> RawMessage:
    return RawMessage.from_bytes((FIXTURES / "abc" / f"{name}.eml").read_bytes())


def parse_one(msg: RawMessage):
    (bill,) = parser.parse(msg)
    return bill


def with_html(msg: RawMessage, old: str, new: str) -> RawMessage:
    assert old in msg.html
    return dataclasses.replace(msg, html_parts=[msg.html.replace(old, new)])


@pytest.mark.parametrize("path", ABC, ids=lambda p: p.stem)
def test_snapshot(path, snapshot):
    bill = parse_one(RawMessage.from_bytes(path.read_bytes()))
    snapshot(f"abc/{path.stem}", bill.model_dump(mode="json"))


@pytest.mark.parametrize("path", ABC, ids=lambda p: p.stem)
def test_reconciles_ok(path):
    bill = parse_one(RawMessage.from_bytes(path.read_bytes()))
    assert bill.status == "OK", bill.warnings
    assert bill.warnings == []


def test_matches_only_abc():
    for path in sorted(FIXTURES.rglob("*.eml")):
        msg = RawMessage.from_bytes(path.read_bytes())
        assert parser.matches(msg) == (path.parent.name == "abc"), path.name


@pytest.mark.parametrize(
    ("name", "currency", "prev", "prev_dep", "charges", "credits", "adjust", "due", "dep"),
    [
        # docs/banks/abc.md §8
        ("abc_mc_2026-09", "CNY", "0.00", "0.00", "371.53", "371.53", "0.00", "0.00", "0.00"),
        ("abc_mc_2026-09", "USD", "55.20", "0.28", "56.89", "55.48", "0.00", "56.33", "0.00"),
        ("abc_visa_2026-09", "CNY", "0.00", "0.00", "19168.54", "19168.54", "0.00", "0.00", "0.00"),
        (
            "abc_visa_2026-09",
            "USD",
            "2836.84",
            "3.29",
            "2285.06",
            "2866.34",
            "0.00",
            "2252.79",
            "0.52",
        ),
        (
            "abc_unionpay_2026-09",
            "CNY",
            "1209.28",
            "0.00",
            "1218.59",
            "1209.28",
            "-0.62",
            "1217.97",
            "0.00",
        ),
    ],
)
def test_summary_block(name, currency, prev, prev_dep, charges, credits, adjust, due, dep):
    bill = parse_one(load(name))
    (b,) = [b for b in bill.balances if b.currency == currency]
    assert (b.previous_balance, b.previous_deposit, b.new_charges, b.payments_credits) == (
        D(prev),
        D(prev_dep),
        D(charges),
        D(credits),
    )
    assert (b.adjustments, b.amount_due, b.deposit) == (D(adjust), D(due), D(dep))


def test_statement_fields():
    bill = parse_one(load("abc_unionpay_2026-09"))
    assert bill.account_id == "ABC:0003" and bill.cards == ["0003"]
    assert str(bill.period_start) == "2026-08-17" and str(bill.period_end) == "2026-09-16"
    assert bill.statement_date == bill.period_end  # ABC prints no separate statement date
    assert str(bill.due_date) == "2026-10-05"
    assert str(bill.email_date) == "2026-09-18"
    assert bill.balances[0].min_payment == D("781.55")


def test_unlisted_adjustment_becomes_synthetic_row():
    bill = parse_one(load("abc_unionpay_2026-09"))
    synthetic = [t for t in bill.transactions if t.synthetic]
    assert len(synthetic) == 1
    (s,) = synthetic
    assert s.amount == D("-0.62") and s.txn_type == TxnType.ADJUSTMENT
    assert s.description_raw == SYNTHETIC_DESCRIPTION and s.line_no == len(bill.transactions)
    # Sum of all rows now equals the net change of the account.
    b = bill.balances[0]
    net = (b.amount_due - b.deposit) - (b.previous_balance - b.previous_deposit)
    assert sum(t.amount for t in bill.transactions) == net


def test_spending_is_positive_and_foreign_amounts_kept():
    bill = parse_one(load("abc_mc_2026-09"))
    purchases = [t for t in bill.transactions if t.txn_type == TxnType.PURCHASE]
    assert [t.amount for t in purchases] == [D("28.25"), D("28.64")]
    assert all(t.currency == "USD" and t.orig_currency == "AUD" for t in purchases)
    assert purchases[0].orig_amount == D("39.90")
    rebates = [t for t in bill.transactions if t.txn_type == TxnType.REBATE]
    assert [t.amount for t in rebates] == [D("-0.28"), D("-0.28")]


def test_auto_fx_repayment_chain():
    bill = parse_one(load("abc_mc_2026-09"))
    by_type = {(t.txn_type, t.currency): t for t in bill.transactions}
    repayment = by_type[(TxnType.REPAYMENT, "CNY")]
    fx_out = by_type[(TxnType.FX_TRANSFER, "CNY")]
    fx_in = by_type[(TxnType.FX_TRANSFER, "USD")]
    assert repayment.amount == D("-371.53")  # money in from the debit card
    assert fx_out.amount == D("371.53")  # CNY account pays for the USD
    assert fx_in.amount == D("-54.92") and fx_in.fx_rate == D("6.76485")
    assert fx_out.card_last4 is None


def test_installment_rows():
    bill = parse_one(load("abc_unionpay_2026-09"))
    principal = next(t for t in bill.transactions if t.txn_type == TxnType.INSTALLMENT)
    interest = next(t for t in bill.transactions if t.txn_type == TxnType.INTEREST)
    assert (principal.amount, principal.installment) == (D("659.00"), "3/36")
    assert (interest.amount, interest.installment) == (D("99.59"), "3/36")


@pytest.mark.parametrize(
    ("description", "merchant", "location"),
    [
        ("GREATER UNION SYDNEY SYDNEY AU", "GREATER UNION SYDNEY", "SYDNEY AU"),
        # 23-char merchant runs straight into the city: must be cut by position.
        ("Red Chilli Noodle No/88Sydney AU", "Red Chilli Noodle No/88", "Sydney AU"),
    ],
)
def test_visa_fixed_width_merchant(description, merchant, location):
    bill = parse_one(load("abc_visa_2026-09"))
    txn = next(t for t in bill.transactions if t.description_raw == f"境外消费 {description}")
    assert (txn.merchant, txn.merchant_location) == (merchant, location)


def test_mastercard_and_online_merchants():
    mc = parse_one(load("abc_mc_2026-09"))
    txn = next(t for t in mc.transactions if t.txn_type == TxnType.PURCHASE)
    assert (txn.merchant, txn.merchant_location) == ("PlusFitness SYDNEY", "AUS")
    up = parse_one(load("abc_unionpay_2026-09"))
    txn = next(t for t in up.transactions if t.txn_type == TxnType.PURCHASE)
    assert txn.merchant == "财付通，深圳市腾讯计算机系统有限公司"


def test_txn_ids_unique_within_bill():
    bill = parse_one(load("abc_visa_2026-09"))
    ids = [t.txn_id for t in bill.transactions]
    assert len(ids) == len(set(ids)) == 119


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (">账务说明<", "><"),  # summary section title
        (">交易明细<", "><"),  # transactions section title
        ("交易日期", "日期"),  # transactions header
        ("536113******0001", "no card"),  # card number
        ("2026/08/02-2026/09/01", ""),  # statement cycle
        ("本期调整金额", "调整"),  # summary header column
    ],
)
def test_missing_key_parts_raise_template_changed(old, new):
    msg = with_html(load("abc_mc_2026-09"), old, new)
    with pytest.raises(TemplateChanged):
        parser.parse(msg)


def test_no_html_raises_template_changed():
    msg = dataclasses.replace(load("abc_mc_2026-09"), html_parts=[])
    with pytest.raises(TemplateChanged):
        parser.parse(msg)


def test_unknown_transaction_is_flagged():
    msg = with_html(load("abc_mc_2026-09"), "MASTER返现", "MASTER某某")
    bill = parse_one(msg)
    assert bill.status == "WARN"
    assert any("未知的交易类型" in w for w in bill.warnings)


def test_cross_check_with_account_info():
    msg = with_html(load("abc_mc_2026-09"), "-56.33", "-56.34")
    bill = parse_one(msg)
    assert bill.status == "WARN"
    assert any("账户信息区" in w for w in bill.warnings)


def test_unparseable_amount_is_a_warning_not_a_crash():
    msg = with_html(load("abc_mc_2026-09"), "-28.25/USD", "-28.25/??")
    bill = parse_one(msg)
    assert bill.status == "WARN"
    assert any("明细行解析失败" in w for w in bill.warnings)
