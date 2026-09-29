"""What a row of the e-mails opens to: the spending behind a category or a shop
(docs/notify.md#邮件内容).

Only spending is listed: purchases, fees, interest and cash, the types the categories and
the shops add up, in the same CNY at the same rates as build_view. So the lines a row opens
to always add up to the row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from autobill.categorize import Rules
from autobill.fx import FxRates, RateUnavailable
from autobill.model import ZERO, Bill
from autobill.report.monthly import SPENDING_TYPES, cents
from autobill.report.statement import _line
from autobill.report.style import BANK_NAMES, money


@dataclass
class Spent:
    """One spending transaction, as the reports count it."""

    day: date
    category: str
    shop: str  # the reports' key: the parser's merchant name, else the description
    value: Decimal  # CNY
    card: str  # "农业银行 0001"
    title: str  # the shop as a list shows it
    local: str  # "A$24.00": what the shop charged
    cny: str  # "≈¥112.53" when that was not yuan


@dataclass
class Line:
    title: str
    sub: str
    local: str
    cny: str


@dataclass
class Lines:
    """A row's lines, oldest first, and what they add up to."""

    lines: list[Line] = field(default_factory=list)
    total: Decimal = ZERO

    @property
    def count(self) -> int:
        return len(self.lines)

    @property
    def total_text(self) -> str:
        return f"¥{money(cents(self.total))}"


def day_text(day: date) -> str:
    return f"{day.month}月{day.day}日"


def rates(bill: Bill, fx: FxRates) -> dict[str, Decimal]:
    """CNY per unit of each currency on the bill, as build_view takes them; a currency
    without a rate is left out, as build_view leaves it out of the totals."""
    out: dict[str, Decimal] = {}
    for currency in {t.currency for t in bill.transactions}:
        try:
            out[currency] = fx.rate(currency, bill.email_date).rate_to_cny
        except RateUnavailable:
            continue
    return out


def spent(bill: Bill, fx: FxRates, rules: Rules, transactions=None) -> list[Spent]:
    """The bill's spending (or that of `transactions`, a part of it), oldest first."""
    have = rates(bill, fx)
    card = f"{BANK_NAMES.get(bill.bank, bill.bank)} {bill.account_id.partition(':')[2]}"
    out = []
    for t in sorted(transactions or bill.transactions, key=lambda t: (t.trans_date, t.line_no)):
        if t.txn_type not in SPENDING_TYPES or t.currency not in have:
            continue
        category = rules.categorize(t.description_raw, t.txn_type, t.merchant)
        value = t.amount * have[t.currency]
        by_ai = rules.by_ai(t.description_raw, t.txn_type, t.merchant)
        line = _line(t, category, False, False, by_ai, card, value)
        shop = t.merchant or t.description_raw
        out.append(
            Spent(t.trans_date, category, shop, value, card, line.title, line.local, line.cny)
        )
    return out


def _lines(items: list[Spent], by_shop: bool) -> Lines:
    """By category the shop is a line's title; by shop the date is."""
    out = Lines()
    for s in sorted(items, key=lambda s: (s.day, s.card)):
        if by_shop:
            title, sub = day_text(s.day), f"{s.card} · {s.category}"
        else:
            title, sub = s.title, f"{day_text(s.day)} · {s.card}"
        out.lines.append(Line(title, sub, s.local, s.cny))
        out.total += s.value
    return out


def by_category(items: list[Spent]) -> dict[str, Lines]:
    groups: dict[str, list[Spent]] = {}
    for s in items:
        groups.setdefault(s.category, []).append(s)
    return {k: _lines(v, by_shop=False) for k, v in groups.items()}


def by_shop(items: list[Spent]) -> dict[str, Lines]:
    groups: dict[str, list[Spent]] = {}
    for s in items:
        groups.setdefault(s.shop, []).append(s)
    return {k: _lines(v, by_shop=True) for k, v in groups.items()}
