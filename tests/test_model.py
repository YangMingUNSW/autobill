from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from autobill.model import Bill, BillBalance, Transaction, TxnType, make_txn_id

D = Decimal


def make_txn(**overrides) -> Transaction:
    fields = dict(
        line_no=1,
        txn_id=make_txn_id("ABC", "ABC:0001", date(2026, 8, 3), D("28.25"), "WOOLWORTHS", 1),
        trans_date=date(2026, 8, 3),
        post_date=date(2026, 8, 5),
        txn_type=TxnType.PURCHASE,
        amount=D("28.25"),
        currency="USD",
        orig_amount=D("39.90"),
        orig_currency="AUD",
        description_raw="WOOLWORTHS",
        card_last4="0001",
    )
    fields.update(overrides)
    return Transaction(**fields)


def make_balance(**overrides) -> BillBalance:
    fields = dict(
        currency="USD",
        previous_balance=D("0"),
        previous_deposit=D("1.13"),
        new_charges=D("100.00"),
        payments_credits=D("50.00"),
        amount_due=D("48.87"),
    )
    fields.update(overrides)
    return BillBalance(**fields)


def make_bill(**overrides) -> Bill:
    fields = dict(
        bank="ABC",
        account_id="ABC:0001",
        cards=["0001"],
        statement_date=date(2026, 9, 1),
        period_start=date(2026, 8, 2),
        period_end=date(2026, 9, 1),
        due_date=date(2026, 9, 26),
        email_date=date(2026, 9, 2),
        balances=[make_balance()],
        transactions=[make_txn()],
        status="OK",
        source_message_id="<x@example.invalid>",
        source_sha256="0" * 64,
        parser_name="abc_html",
        parser_version=1,
    )
    fields.update(overrides)
    return Bill(**fields)


def test_build_a_complete_bill():
    bill = make_bill()
    assert bill.transactions[0].amount == D("28.25")
    assert bill.balances[0].deposit == D("0")
    assert bill.warnings == [] and bill.quality == 3 and bill.reported_at is None


def test_bill_json_round_trip_keeps_decimals():
    bill = make_bill()
    again = Bill.model_validate_json(bill.model_dump_json())
    assert again == bill
    assert isinstance(again.transactions[0].amount, Decimal)


@pytest.mark.parametrize("bad", [28.25, 28, "28.25"])
def test_amounts_must_be_decimal(bad):
    with pytest.raises(ValidationError):
        make_txn(amount=bad)


def test_float_rejected_on_assignment_too():
    txn = make_txn()
    with pytest.raises(ValidationError):
        txn.amount = 1.5


@pytest.mark.parametrize(
    "field", ["previous_balance", "previous_deposit", "payments_credits", "amount_due", "deposit"]
)
def test_non_negative_balance_fields(field):
    with pytest.raises(ValidationError):
        make_balance(**{field: D("-0.01")})


def test_adjustments_may_be_negative():
    assert make_balance(adjustments=D("-0.62")).adjustments == D("-0.62")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currency", "usd"),
        ("currency", "US"),
        ("card_last4", "12345"),
        ("installment", "3 of 36"),
        ("unknown_field", 1),  # typos must not be silently ignored
    ],
)
def test_transaction_field_validation(field, value):
    with pytest.raises(ValidationError):
        make_txn(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "ABC-0001"),
        ("account_id", "ABC:1"),
        ("status", "FAILED"),  # FAILED is an e-mail state, never a Bill state
        ("source_sha256", "abc"),
        ("parser_version", 0),
    ],
)
def test_bill_field_validation(field, value):
    with pytest.raises(ValidationError):
        make_bill(**{field: value})


def test_ccb_unknown_account_is_allowed():
    assert make_bill(bank="CCB", account_id="CCB:unknown", cards=[]).account_id == "CCB:unknown"


def test_due_date_may_be_empty():
    assert make_bill(due_date=None).due_date is None  # BOC, nothing due


def test_txn_id_is_stable_and_normalises_amounts():
    args = ("ABC", "ABC:0001", date(2026, 8, 3), D("28.25"), "WOOLWORTHS", 1)
    first = make_txn_id(*args)
    assert first == make_txn_id(*args)
    assert len(first) == 16 and int(first, 16) >= 0
    assert make_txn_id("ABC", "ABC:0001", date(2026, 8, 3), D("28.250"), "WOOLWORTHS", 1) == first


@pytest.mark.parametrize(
    "changed",
    [
        ("CCB", "ABC:0001", date(2026, 8, 3), D("28.25"), "WOOLWORTHS", 1),
        ("ABC", "ABC:0002", date(2026, 8, 3), D("28.25"), "WOOLWORTHS", 1),
        ("ABC", "ABC:0001", date(2026, 8, 4), D("28.25"), "WOOLWORTHS", 1),
        ("ABC", "ABC:0001", date(2026, 8, 3), D("-28.25"), "WOOLWORTHS", 1),
        ("ABC", "ABC:0001", date(2026, 8, 3), D("28.25"), "COLES", 1),
        ("ABC", "ABC:0001", date(2026, 8, 3), D("28.25"), "WOOLWORTHS", 2),
    ],
)
def test_txn_id_changes_with_any_field(changed):
    base = make_txn_id("ABC", "ABC:0001", date(2026, 8, 3), D("28.25"), "WOOLWORTHS", 1)
    assert make_txn_id(*changed) != base


def test_txn_id_rejects_float():
    with pytest.raises(TypeError):
        make_txn_id("ABC", "ABC:0001", date(2026, 8, 3), 28.25, "WOOLWORTHS", 1)
