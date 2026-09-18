"""Monthly spending summary (terminal version, M3). See docs/notify.md#统计口径.

Spending counts PURCHASE, FEE, INTEREST and CASH; REFUND and REBATE reduce it; instalment
principal, repayments, FX transfers and adjustments are not spending. Transactions belong
to the calendar month of their transaction date. Each bill's amounts are converted to CNY
with the rate of that bill's e-mail date, so one card can contribute several lines.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from autobill.fx import FxRates, Rate, RateUnavailable
from autobill.model import ZERO, TxnType

SPENDING_TYPES = [TxnType.PURCHASE, TxnType.FEE, TxnType.INTEREST, TxnType.CASH]
REDUCING_TYPES = [TxnType.REFUND, TxnType.REBATE]  # stored negative, so a plain sum subtracts
CENT = Decimal("0.01")


@dataclass
class Line:
    """Spending of one currency on one bill within the month."""

    account_id: str
    statement_date: date
    status: str  # the bill's reconciliation status
    currency: str
    spend: Decimal
    rate: Rate | None  # None when no rate could be found at all
    cny: Decimal | None


@dataclass
class MonthlySummary:
    month: str  # "2026-08"
    lines: list[Line] = field(default_factory=list)

    @property
    def by_currency(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for line in self.lines:
            totals[line.currency] = totals.get(line.currency, ZERO) + line.spend
        return dict(sorted(totals.items()))

    @property
    def total_cny(self) -> Decimal:
        return sum((line.cny for line in self.lines if line.cny is not None), ZERO)

    @property
    def complete_conversion(self) -> bool:
        return all(line.cny is not None for line in self.lines)


def month_bounds(month: str) -> tuple[date, date]:
    """ "2026-08" -> (2026-08-01, 2026-09-01): start inclusive, end exclusive."""
    try:
        year, mon = (int(x) for x in month.split("-"))
        start = date(year, mon, 1)
    except ValueError:
        raise ValueError(f"month must look like 2026-08, got {month!r}") from None
    end = date(year + (mon == 12), mon % 12 + 1, 1)
    return start, end


def monthly_summary(conn: sqlite3.Connection, month: str, fx: FxRates) -> MonthlySummary:
    start, end = month_bounds(month)
    types = [t.value for t in SPENDING_TYPES + REDUCING_TYPES]
    rows = conn.execute(
        f"""SELECT b.account_id, b.statement_date, b.status, b.email_date, t.currency, t.amount
            FROM transactions t JOIN bills b ON b.id = t.bill_id
            WHERE t.trans_date >= ? AND t.trans_date < ?
              AND t.txn_type IN ({", ".join("?" * len(types))})
            ORDER BY b.account_id, b.statement_date, t.currency""",
        (start.isoformat(), end.isoformat(), *types),
    ).fetchall()

    # Sum in Python with Decimal (amounts are stored as exact text).
    groups: dict[tuple, Decimal] = {}
    for r in rows:
        key = (r["account_id"], r["statement_date"], r["status"], r["email_date"], r["currency"])
        groups[key] = groups.get(key, ZERO) + Decimal(r["amount"])

    summary = MonthlySummary(month)
    for (account_id, statement_date, status, email_date, currency), spend in groups.items():
        try:
            rate = fx.rate(currency, date.fromisoformat(email_date))
            cny = (spend * rate.rate_to_cny).quantize(CENT, rounding=ROUND_HALF_UP)
        except RateUnavailable:
            rate, cny = None, None
        summary.lines.append(
            Line(account_id, date.fromisoformat(statement_date), status, currency, spend, rate, cny)
        )
    return summary


STATUS_NOTE = {"OK": "", "WARN": "  ⚠ 对账有警告", "UNVERIFIED": "  未对账"}
SOURCE_NOTE = {"frankfurter": "", "config": "，配置汇率", "identity": ""}


def _width(text: str) -> int:
    """Terminal display width: CJK and full-width characters take two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    gap = " " * max(width - _width(text), 0)
    return gap + text if right else text + gap


COLUMNS = [("账户", 10, False), ("账单日", 10, False), ("币种", 4, False), ("支出", 12, True),
           ("汇率（日期）", 26, False), ("人民币", 12, True)]  # fmt: skip


def _row(cells: list[str]) -> str:
    return "  ".join(_pad(c, w, right) for c, (_, w, right) in zip(cells, COLUMNS, strict=True))


def render_text(summary: MonthlySummary) -> str:
    """Plain-text rendering for the terminal."""
    out = [f"{summary.month} 支出汇总（按交易日归月，不含分期本金、还款、购汇）", ""]
    if not summary.lines:
        out.append("这个月没有支出数据。")
        return "\n".join(out)

    out.append(_row([name for name, _, _ in COLUMNS]))
    for line in summary.lines:
        if line.rate is None:
            rate_text, cny_text = "无汇率", "—"
        elif line.rate.source == "identity":
            rate_text, cny_text = "—", f"{line.cny:,.2f}"
        else:
            day = line.rate.rate_date.isoformat() if line.rate.rate_date else "配置"
            rate_text = f"{line.rate.rate_to_cny}（{day}{SOURCE_NOTE[line.rate.source]}）"
            cny_text = f"{line.cny:,.2f}"
        cells = [line.account_id, line.statement_date.isoformat(), line.currency,
                 f"{line.spend:,.2f}", rate_text, cny_text]  # fmt: skip
        out.append(_row(cells) + STATUS_NOTE.get(line.status, ""))
    out.append("")
    out.append("分币种合计：" + "；".join(f"{c} {v:,.2f}" for c, v in summary.by_currency.items()))
    out.append(f"人民币合计：{summary.total_cny:,.2f}")
    if not summary.complete_conversion:
        out.append("注意：有币种取不到汇率，没有计入人民币合计。")
    out.append("汇率：每份账单按它的邮件发出当天折算（周末取前一个工作日），来源 Frankfurter。")
    return "\n".join(out)
