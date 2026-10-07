"""The counting rules every report shares (docs/notify.md#统计口径): which transactions are
spending, how money is rounded, and which month a date or a statement belongs to.

Spending is PURCHASE, FEE, INTEREST and CASH; REFUND and REBATE take it down. Instalment
principal, repayments, FX transfers and adjustments are not spending: instalment principal
was counted once already, when the purchase was made.

A statement month ("账单月", a cycle) is the month of the statement date, as the banks name
their statements. Statement and due dates are Beijing time, which has no daylight saving.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from autobill.model import TxnType

SPENDING_TYPES = [TxnType.PURCHASE, TxnType.FEE, TxnType.INTEREST, TxnType.CASH]
REDUCING_TYPES = [TxnType.REFUND, TxnType.REBATE]  # stored negative, so a plain sum subtracts
CENT = Decimal("0.01")
CHINA = timezone(timedelta(hours=8))  # statement and due dates are Beijing time (no DST)


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def today_in_china() -> date:
    return datetime.now(CHINA).date()


def month_bounds(month: str) -> tuple[date, date]:
    """ "2026-08" -> (2026-08-01, 2026-09-01): start inclusive, end exclusive."""
    try:
        year, mon = (int(x) for x in month.split("-"))
        start = date(year, mon, 1)
    except ValueError:
        raise ValueError(f"month must look like 2026-08, got {month!r}") from None
    end = date(year + (mon == 12), mon % 12 + 1, 1)
    return start, end


def cycle_of(statement_date: date) -> str:
    """The statement month a statement belongs to: "2026-09"."""
    return statement_date.strftime("%Y-%m")


def previous_cycle(cycle: str) -> str:
    start, _ = month_bounds(cycle)
    return (start - timedelta(days=1)).strftime("%Y-%m")


def earlier_cycles(cycle: str, count: int) -> list[str]:
    """The `count` statement months before `cycle`, oldest first."""
    cycles: list[str] = []
    while len(cycles) < count:
        cycles.insert(0, previous_cycle(cycles[0] if cycles else cycle))
    return cycles
