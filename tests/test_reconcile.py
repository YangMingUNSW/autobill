"""Three-step reconciliation on hand-built bills (docs/data-model.md#对账)."""

from datetime import date
from decimal import Decimal

from autobill.model import Bill, BillBalance, Transaction, TxnType, make_txn_id
from autobill.reconcile import SYNTHETIC_DESCRIPTION, reconcile

D = Decimal


def txn(line_no: int, amount: str, txn_type=TxnType.PURCHASE, currency="CNY") -> Transaction:
    return Transaction(
        line_no=line_no,
        txn_id=make_txn_id("ABC", "ABC:0001", date(2026, 8, 5), D(amount), "x", line_no),
        trans_date=date(2026, 8, 5),
        txn_type=txn_type,
        amount=D(amount),
        currency=currency,
        description_raw="x",
    )


def bill(balances, transactions, warnings=()) -> Bill:
    return Bill(
        bank="ABC",
        account_id="ABC:0001",
        cards=["0001"],
        statement_date=date(2026, 9, 1),
        email_date=date(2026, 9, 2),
        balances=balances,
        transactions=transactions,
        status="OK",
        warnings=list(warnings),
        source_message_id="<x>",
        source_sha256="0" * 64,
        parser_name="test",
        parser_version=1,
    )


def balance(**kw) -> BillBalance:
    fields = dict(
        currency="CNY",
        previous_balance=D("100.00"),
        new_charges=D("30.00"),
        payments_credits=D("100.00"),
        amount_due=D("30.00"),
    )
    fields.update({k: v if k == "currency" else D(v) for k, v in kw.items()})
    return BillBalance(**fields)


def test_all_steps_pass():
    result = reconcile(bill([balance()], [txn(1, "30.00"), txn(2, "-100.00", TxnType.REPAYMENT)]))
    assert result.status == "OK" and result.warnings == []
    assert not any(t.synthetic for t in result.transactions)


def test_step1_identity_failure():
    result = reconcile(
        bill([balance(amount_due="31.00")], [txn(1, "30.00"), txn(2, "-100.00", TxnType.REPAYMENT)])
    )
    assert result.status == "WARN"
    assert any("第 1 步" in w and "差 1.00" in w for w in result.warnings)


def test_step2_debit_and_credit_mismatch():
    result = reconcile(bill([balance()], [txn(1, "29.00"), txn(2, "-99.00", TxnType.REPAYMENT)]))
    assert result.status == "WARN"
    assert any("借方" in w and "差 -1.00" in w for w in result.warnings)
    assert any("贷方" in w and "差 -1.00" in w for w in result.warnings)


def test_step3_unlisted_adjustment_gets_synthetic_row():
    b = balance(amount_due="29.38", adjustments="-0.62")
    result = reconcile(bill([b], [txn(1, "30.00"), txn(2, "-100.00", TxnType.REPAYMENT)]))
    assert result.status == "OK", result.warnings
    (s,) = [t for t in result.transactions if t.synthetic]
    assert (s.amount, s.line_no, s.description_raw) == (D("-0.62"), 3, SYNTHETIC_DESCRIPTION)
    assert s.trans_date == date(2026, 9, 1)


def test_listed_adjustment_needs_no_synthetic_row():
    b = balance(amount_due="29.38", adjustments="-0.62")
    rows = [
        txn(1, "30.00"),
        txn(2, "-100.00", TxnType.REPAYMENT),
        txn(3, "-0.62", TxnType.ADJUSTMENT),
    ]
    result = reconcile(bill([b], rows))
    assert result.status == "OK", result.warnings
    assert not any(t.synthetic for t in result.transactions)


def test_deposits_are_part_of_the_identity():
    # ABC VISA USD: an overpayment carried in and out (docs/banks/abc.md §8).
    b = balance(
        currency="USD",
        previous_balance="2836.84",
        previous_deposit="3.29",
        new_charges="2285.06",
        payments_credits="2866.34",
        amount_due="2252.79",
        deposit="0.52",
    )
    rows = [txn(1, "2285.06", currency="USD"), txn(2, "-2866.34", TxnType.REPAYMENT, "USD")]
    assert reconcile(bill([b], rows)).status == "OK"


def test_currency_missing_from_summary():
    rows = [txn(1, "30.00"), txn(2, "-100.00", TxnType.REPAYMENT), txn(3, "1.00", currency="USD")]
    result = reconcile(bill([balance()], rows))
    assert result.status == "WARN"
    assert any("USD" in w and "汇总块里没有" in w for w in result.warnings)


def test_no_summary_is_unverified():
    result = reconcile(bill([], [txn(1, "30.00")]))
    assert result.status == "UNVERIFIED"


def test_parser_warnings_keep_status_warn():
    rows = [txn(1, "30.00"), txn(2, "-100.00", TxnType.REPAYMENT)]
    result = reconcile(bill([balance()], rows, warnings=["未知的交易类型"]))
    assert result.status == "WARN" and result.warnings == ["未知的交易类型"]


def test_idempotent():
    b = balance(amount_due="29.38", adjustments="-0.62")
    once = reconcile(bill([b], [txn(1, "30.00"), txn(2, "-100.00", TxnType.REPAYMENT)]))
    assert reconcile(once) == once
    bad = reconcile(bill([balance(amount_due="31.00")], [txn(1, "30.00")]))
    assert reconcile(bad) == bad
