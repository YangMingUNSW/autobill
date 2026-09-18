"""Monthly spending summary for the terminal. See docs/notify.md#统计口径.

Spending counts PURCHASE, FEE, INTEREST and CASH; REFUND and REBATE reduce it; instalment
principal, repayments, FX transfers and adjustments are not spending. Transactions belong
to the calendar month of their transaction date. Each bill's amounts are converted to CNY
with the rate of that bill's e-mail date, so one card can contribute several lines.

Besides the per-bill lines (M3) the summary has categories, rebates/refunds, spending per
card, a six-month trend, top merchants and uncategorised merchants (M6). Only totals are
shown, never single transactions.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from autobill.categorize import UNCATEGORISED, Rules, load_rules
from autobill.fx import FxRates, Rate, RateUnavailable
from autobill.model import ZERO, TxnType

SPENDING_TYPES = [TxnType.PURCHASE, TxnType.FEE, TxnType.INTEREST, TxnType.CASH]
REDUCING_TYPES = [TxnType.REFUND, TxnType.REBATE]  # stored negative, so a plain sum subtracts
CREDIT_NAMES = {TxnType.REBATE: "返现", TxnType.REFUND: "退款"}
CENT = Decimal("0.01")
TREND_MONTHS = 6
TOP_MERCHANTS = 10
TOP_UNCATEGORISED = 10


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


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
    categories: dict[str, Decimal] = field(default_factory=dict)  # CNY, spending only
    credits: dict[str, Decimal] = field(default_factory=dict)  # CNY, negative: 返现/退款
    top_merchants: list[tuple[str, Decimal]] = field(default_factory=list)  # CNY
    uncategorised: list[tuple[str, int, Decimal]] = field(default_factory=list)  # name, n, CNY
    trend: list[tuple[str, Decimal | None]] = field(default_factory=list)  # None = no data

    @property
    def by_currency(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for line in self.lines:
            totals[line.currency] = totals.get(line.currency, ZERO) + line.spend
        return dict(sorted(totals.items()))

    @property
    def by_account(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for line in self.lines:
            if line.cny is not None:
                totals[line.account_id] = totals.get(line.account_id, ZERO) + line.cny
        return totals

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


def previous_months(month: str, count: int) -> list[str]:
    """The `count` months ending with `month`, oldest first."""
    start, _ = month_bounds(month)
    index = start.year * 12 + start.month - 1
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(index - count + 1, index + 1)]


def _rows(conn: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    start, end = month_bounds(month)
    types = [t.value for t in SPENDING_TYPES + REDUCING_TYPES]
    return conn.execute(
        f"""SELECT b.account_id, b.statement_date, b.status, b.email_date,
                   t.currency, t.amount, t.txn_type, t.description_raw, t.merchant
            FROM transactions t JOIN bills b ON b.id = t.bill_id
            WHERE t.trans_date >= ? AND t.trans_date < ?
              AND t.txn_type IN ({", ".join("?" * len(types))})
            ORDER BY b.account_id, b.statement_date, t.currency, t.line_no""",
        (start.isoformat(), end.isoformat(), *types),
    ).fetchall()


def _lines(rows: list[sqlite3.Row], fx: FxRates) -> tuple[list[Line], dict[tuple, Rate | None]]:
    """Per bill and currency: spending in the original currency and in CNY."""
    groups: dict[tuple, Decimal] = {}
    for r in rows:  # sum in Python with Decimal: amounts are stored as exact text
        key = (r["account_id"], r["statement_date"], r["status"], r["email_date"], r["currency"])
        groups[key] = groups.get(key, ZERO) + Decimal(r["amount"])
    lines, rates = [], {}
    for (account_id, statement_date, status, email_date, currency), spend in groups.items():
        try:
            rate = fx.rate(currency, date.fromisoformat(email_date))
            cny = cents(spend * rate.rate_to_cny)
        except RateUnavailable:
            rate, cny = None, None
        rates[(email_date, currency)] = rate
        lines.append(
            Line(account_id, date.fromisoformat(statement_date), status, currency, spend, rate, cny)
        )
    return lines, rates


def monthly_summary(
    conn: sqlite3.Connection, month: str, fx: FxRates, rules: Rules | None = None
) -> MonthlySummary:
    rules = rules or load_rules()
    rows = _rows(conn, month)
    lines, rates = _lines(rows, fx)
    summary = MonthlySummary(month, lines)

    categories: dict[str, Decimal] = defaultdict(lambda: ZERO)
    credits: dict[str, Decimal] = defaultdict(lambda: ZERO)
    merchants: dict[str, Decimal] = defaultdict(lambda: ZERO)
    uncategorised: dict[str, list] = defaultdict(lambda: [0, ZERO])
    for r in rows:
        rate = rates[(r["email_date"], r["currency"])]
        if rate is None:
            continue  # shown as "无汇率" in the lines; left out of every CNY total
        cny = Decimal(r["amount"]) * rate.rate_to_cny
        txn_type = TxnType(r["txn_type"])
        if txn_type in CREDIT_NAMES:
            credits[CREDIT_NAMES[txn_type]] += cny
            continue
        category = rules.categorize(r["description_raw"], txn_type)
        categories[category] += cny
        if txn_type == TxnType.PURCHASE:
            name = r["merchant"] or r["description_raw"]
            merchants[name] += cny
            if category == UNCATEGORISED:
                uncategorised[name][0] += 1
                uncategorised[name][1] += cny

    summary.categories = {k: cents(v) for k, v in sorted(categories.items(), key=lambda kv: -kv[1])}
    summary.credits = {k: cents(v) for k, v in credits.items()}
    summary.top_merchants = [
        (name, cents(v)) for name, v in sorted(merchants.items(), key=lambda kv: -kv[1])
    ][:TOP_MERCHANTS]
    summary.uncategorised = [
        (name, n, cents(v))
        for name, (n, v) in sorted(uncategorised.items(), key=lambda kv: (-kv[1][1], kv[0]))
    ][:TOP_UNCATEGORISED]
    for m in previous_months(month, TREND_MONTHS):
        month_lines = lines if m == month else _lines(_rows(conn, m), fx)[0]
        converted = [line.cny for line in month_lines if line.cny is not None]
        summary.trend.append((m, sum(converted, ZERO) if converted else None))
    return summary


# --- terminal rendering ---------------------------------------------------------

STATUS_NOTE = {"OK": "", "WARN": "  ⚠ 对账有警告", "UNVERIFIED": "  未对账"}
SOURCE_NOTE = {"frankfurter": "", "config": "，配置汇率", "identity": ""}


def _width(text: str) -> int:
    """Terminal display width: CJK and full-width characters take two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    gap = " " * max(width - _width(text), 0)
    return gap + text if right else text + gap


def _clip(text: str, width: int) -> str:
    out = ""
    for ch in text:
        if _width(out + ch) > width - 1:
            return out + "…"
        out += ch
    return out


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

    out.append("一、各卡明细")
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

    out += ["", "二、分类（人民币）"]
    spending = sum(summary.categories.values(), ZERO)
    for name, value in summary.categories.items():
        share = f"{value / spending * 100:.1f}%" if spending else "—"
        out.append(f"  {_pad(name, 22)}{_pad(f'{value:,.2f}', 12, True)}  {_pad(share, 6, True)}")
    for name, value in summary.credits.items():
        out.append(f"  {_pad(name + '（扣减）', 22)}{_pad(f'{value:,.2f}', 12, True)}")

    out += ["", "三、按卡"]
    for account, value in summary.by_account.items():
        out.append(f"  {_pad(account, 22)}{_pad(f'{value:,.2f}', 12, True)}")

    out += ["", f"四、近 {TREND_MONTHS} 个月"]
    for month, value in summary.trend:
        text = f"{value:,.2f}" if value is not None else "—（没有数据）"
        mark = "  ← 本月" if month == summary.month else ""
        out.append(f"  {_pad(month, 22)}{_pad(text, 12, True)}{mark}")

    out += ["", f"五、支出最多的商户（前 {TOP_MERCHANTS} 名）"]
    for i, (name, value) in enumerate(summary.top_merchants, start=1):
        out.append(f"  {i:>2}. {_pad(_clip(name, 30), 30)}{_pad(f'{value:,.2f}', 12, True)}")

    out += ["", "六、未分类的商户（可以补进 rules.yaml）"]
    if not summary.uncategorised:
        out.append("  没有，所有消费都分好类了。")
    for name, count, value in summary.uncategorised:
        out.append(
            f"  {_pad(_clip(name, 30), 30)}{_pad(f'{count} 笔', 6, True)}"
            f"{_pad(f'{value:,.2f}', 12, True)}"
        )

    out += [
        "",
        "说明：汇率按每份账单的邮件发出当天折算（周末取前一个工作日），来源 Frankfurter；"
        "分项各自四舍五入，和合计可能差几分钱。",
    ]
    return "\n".join(out)
