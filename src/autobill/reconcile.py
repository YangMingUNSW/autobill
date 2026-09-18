"""Itemised reconciliation of a bill against its own summary block.

See docs/data-model.md#对账. Each currency is checked separately, with exact Decimal
comparison, in three steps:

1. The summary identity the bank prints on the statement.
2. Debits and credits of the listed transactions against the summary totals
   (ADJUSTMENT rows are excluded here: banks report adjustments in their own column).
3. Adjustments without a matching listed row become one synthetic ADJUSTMENT
   transaction, so that the sum of all rows always equals the net change.

Warnings are written in Chinese because they end up in the user's report.
"""

from __future__ import annotations

from decimal import Decimal

from autobill.model import ZERO, Bill, BillBalance, Transaction, TxnType, make_txn_id

SYNTHETIC_DESCRIPTION = "账单调整（未列明细）"


def reconcile(bill: Bill) -> Bill:
    """Return a copy of the bill with status, warnings and synthetic rows filled in.

    Idempotent: synthetic rows from an earlier run are dropped and recomputed.
    Warnings already on the bill (e.g. from the parser) keep its status at WARN.
    """
    rows = [t for t in bill.transactions if not t.synthetic]
    warnings = [w for w in bill.warnings if not w.startswith(_PREFIX)]
    parser_warned = bool(warnings)

    if not bill.balances:
        return bill.model_copy(
            update={
                "transactions": rows,
                "status": "WARN" if parser_warned else "UNVERIFIED",
                "warnings": [*warnings, f"{_PREFIX}没有汇总块，未对账"],
            }
        )

    problems: list[str] = []
    for currency in sorted({t.currency for t in rows} - {b.currency for b in bill.balances}):
        problems.append(f"{_PREFIX}{currency} 有流水，但汇总块里没有这个币种")

    synthetic: list[Transaction] = []
    next_line = max((t.line_no for t in rows), default=0) + 1
    for balance in bill.balances:
        own = [t for t in rows if t.currency == balance.currency]
        problems += _check_identity(balance)
        problems += _check_items(balance, own)
        diff = balance.adjustments - _sum(t.amount for t in own if t.txn_type == TxnType.ADJUSTMENT)
        if diff != 0:
            synthetic.append(_synthetic_adjustment(bill, balance.currency, diff, next_line))
            next_line += 1

    return bill.model_copy(
        update={
            "transactions": rows + synthetic,
            "status": "WARN" if problems or parser_warned else "OK",
            "warnings": warnings + problems,
        }
    )


_PREFIX = "对账："


def _sum(values) -> Decimal:
    return sum(values, ZERO)


def _check_identity(b: BillBalance) -> list[str]:
    """Step 1: (amount_due - deposit) = (previous - previous_deposit) + charges + interest
    - credits + adjustments."""
    left = b.amount_due - b.deposit
    right = (
        b.previous_balance
        - b.previous_deposit
        + b.new_charges
        + b.interest_fees
        - b.payments_credits
        + b.adjustments
    )
    if left == right:
        return []
    return [
        f"{_PREFIX}{b.currency} 第 1 步汇总恒等式不成立："
        f"本期欠款-溢缴款 = {left}，按算式应为 {right}，差 {left - right}"
    ]


def _check_items(b: BillBalance, rows: list[Transaction]) -> list[str]:
    """Step 2: listed debits and credits against the summary totals."""
    items = [t for t in rows if t.txn_type != TxnType.ADJUSTMENT]
    debits = _sum(t.amount for t in items if t.amount > 0)
    credits = _sum(-t.amount for t in items if t.amount < 0)
    # interest_fees is only non-zero for banks that list it separately in the summary
    # while also itemising it; see docs/data-model.md#对账.
    expected_debits = b.new_charges + b.interest_fees
    problems = []
    if debits != expected_debits:
        problems.append(
            f"{_PREFIX}{b.currency} 第 2 步借方不符：明细合计 {debits}，"
            f"汇总块 {expected_debits}，差 {debits - expected_debits}"
        )
    if credits != b.payments_credits:
        problems.append(
            f"{_PREFIX}{b.currency} 第 2 步贷方不符：明细合计 {credits}，"
            f"汇总块 {b.payments_credits}，差 {credits - b.payments_credits}"
        )
    return problems


def _synthetic_adjustment(bill: Bill, currency: str, amount: Decimal, line_no: int) -> Transaction:
    """Step 3: an adjustment the bank reported in the summary but did not list."""
    return Transaction(
        line_no=line_no,
        txn_id=make_txn_id(
            bill.bank, bill.account_id, bill.statement_date, amount, SYNTHETIC_DESCRIPTION, line_no
        ),
        trans_date=bill.statement_date,
        txn_type=TxnType.ADJUSTMENT,
        amount=amount,
        currency=currency,
        description_raw=SYNTHETIC_DESCRIPTION,
        synthetic=True,
    )
