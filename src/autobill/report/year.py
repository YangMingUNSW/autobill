"""The year in review (docs/notify.md#年度回顾): one calendar year, 1 January to 31 December,
in one e-mail, sent once the January statements after it have brought in December's last
spending. Every transaction goes to the year and month of its own transaction date, the
way `autobill report --month` counts; each is converted at its statement's rate and counted
by build_view exactly as the month's e-mail counts it, so the twelve months add up to the
year. The months here are calendar months, not the month's e-mails' statement months: a
statement issued on 12 January brings 12-31 December to one year and 1-12 January to the
next. The same look as the month's e-mail (templates/_email.css); nothing loaded from
outside, no links, no script.

Over a year one shop shows up under several names, because the bank glues its payment
markers to the name ("Woolworths OnlineAUSVISA Apple Pay", "跨行消费 ..."); the year's
lists put them together under the name without the markers (categories/names.py).

The first three cards filter the year by month and category, two groups of radio buttons
and one CSS rule per value, no script ("punched card coding"): every choice is written into
the e-mail beforehand and the chosen one shows. The bars are one set of twelve that carry
their height for every category and grow or shrink on a spring; a month chosen lists its
lines. It was tried as a prototype on iOS 27 Apple Mail.

This module gathers the year's figures (build_year_report); report/year_explorer.py
builds the filter by month and category, report/year_mail.py the e-mail.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from autobill import build_label
from autobill.categories.names import shown_name
from autobill.categories.rules import TYPE_CATEGORIES, UNCATEGORISED, Rules
from autobill.fx import FxRates, RateUnavailable
from autobill.ledger import CHINA, REDUCING_TYPES, SPENDING_TYPES, cents
from autobill.model import ZERO, Bill, Transaction, TxnType
from autobill.report import drill
from autobill.report.cycle import (
    NAMED,
    MonthBar,
    Segment,
    category_segments,
    latest_bills,
    make_segment,
)
from autobill.report.statement import StatementView, build_view
from autobill.report.style import amount_with_symbol, card_label, emoji_for, money, share
from autobill.report.year_explorer import TOP_MERCHANTS, TOP_VISITS, Explorer, build_explorer
from autobill.store.db import load_bill

REVIEW_UNTIL = (3, 31)  # a year's review goes out in the next year's first quarter, or never
CHARGES = (TYPE_CATEGORIES[TxnType.INTEREST], TYPE_CATEGORIES[TxnType.FEE])  # 利息, 手续费
COUNTED = set(SPENDING_TYPES) | set(REDUCING_TYPES)  # what spending is made of, as everywhere
CURRENCY_NAMES = {
    "CNY": "人民币", "AUD": "澳元", "USD": "美元", "EUR": "欧元", "JPY": "日元", "GBP": "英镑",
    "HKD": "港币", "MOP": "澳门元", "TWD": "新台币", "CHF": "瑞士法郎", "THB": "泰铢",
    "SGD": "新加坡元", "NZD": "新西兰元", "CAD": "加元", "KRW": "韩元", "MYR": "马来西亚林吉特",
}  # fmt: skip


@dataclass
class Visit:
    """A shop you kept going back to: how often, and what it came to in CNY."""

    name: str
    count: int
    amount: str
    emoji: str
    tone: str


@dataclass
class CardYear:
    label: str  # "农业银行 0003"
    bank: str
    last4: str
    amount: str  # CNY spent over the year, counted as 本月消费 is
    share: str


@dataclass
class YearReport:
    year: int
    bars: list[MonthBar]  # January to December by transaction date; None: nothing that month
    spend: Decimal  # the twelve months added up
    purchases: int
    categories: list[Segment]  # every category, largest first; beyond NAMED in grey
    segments: list[Segment]  # the donut: NAMED categories, the rest as 其他, 未分类 last
    merchants: list[Segment]
    visits: list[Visit]
    currencies: list[Segment]  # purchases by the currency they were paid in
    cards: list[CardYear]
    rebates: Decimal  # cashback, a positive amount (spend already has it taken off)
    charges: Decimal  # the 利息 and 手续费 categories: installment interest, fees
    previous: Decimal | None  # the year before, only when all its twelve months are in
    through: date | None  # the last transaction date so far
    complete: bool  # the January statements after the year are in: December is all there
    generated_at: str
    build: str
    explorer: Explorer | None = None  # the e-mail's filter by month and category

    @property
    def title(self) -> str:
        return f"{self.year} 年度回顾"

    @property
    def subject(self) -> str:
        return f"📊 {self.year} 年信用卡年度回顾"

    @property
    def months(self) -> int:
        return sum(1 for b in self.bars if b.value is not None)

    @property
    def span(self) -> str:
        """ "1月到12月" once the year is all in; before that, how far it goes."""
        have = [b for b in self.bars if b.value is not None]
        if not have or self.through is None:
            return "这一年还没有消费"
        text = f"{have[0].label}到{have[-1].label}"
        if not self.complete:
            text += f" · 截至 {self.through.month}月{self.through.day}日"
        return text

    @property
    def spend_total(self) -> str:
        return money(cents(self.spend))

    @property
    def average(self) -> str:
        return f"¥{self.spend / self.months:,.0f}" if self.months else "—"

    @property
    def preview(self) -> str:
        """The grey line Mail shows under the subject."""
        return f"全年消费 ¥{self.spend_total} · 月均 {self.average} · {self.purchases} 笔消费"

    @property
    def change(self) -> str:
        """ "比 2025 年 +12%"; nothing when the year before is not complete."""
        if self.previous is None or self.previous <= 0 or self.spend <= 0:
            return ""
        ratio = (self.spend - self.previous) / self.previous
        return f"比 {self.year - 1} 年 {'+' if ratio >= 0 else '-'}{abs(ratio):.0%}"

    @property
    def trend_label(self) -> str:
        """What a screen reader says for the bars, as trend_svg says it."""
        said = "，".join(
            f"{b.label} " + (f"¥{b.value:,.0f}" if b.value is not None else "无账单")
            for b in self.bars
        )
        return f"{self.year} 年每个月的消费（人民币）：{said}"

    @property
    def trend_caption(self) -> str:
        """ "最多：5月 ¥59,053 · 最少：8月 ¥16,482"."""
        have = [b for b in self.bars if b.value is not None]
        if len(have) < 2:
            return ""
        high = max(have, key=lambda b: b.value)
        low = min(have, key=lambda b: b.value)
        return f"最多：{high.label} ¥{high.value:,.0f} · 最少：{low.label} ¥{low.value:,.0f}"

    @property
    def rebates_text(self) -> str:
        return money(cents(self.rebates))

    @property
    def charges_text(self) -> str:
        return money(cents(self.charges))


def _rates(bill: Bill, fx: FxRates) -> dict[str, Decimal]:
    """CNY per unit of each currency on the bill, the rates build_view uses; a currency
    without a rate is left out, as build_view leaves it out of the totals."""
    rates: dict[str, Decimal] = {}
    for currency in {t.currency for t in bill.transactions}:
        try:
            rates[currency] = fx.rate(currency, bill.email_date).rate_to_cny
        except RateUnavailable:
            continue
    return rates


def _pieces(
    conn: sqlite3.Connection, year: int, fx: FxRates, rules: Rules
) -> Iterator[tuple[Bill, int, list[Transaction], StatementView]]:
    """Each statement's part of the year, month by month: (statement, calendar month, its
    transactions of that month, their view as the month's e-mail counts them). A year's
    transactions are on the statements issued from its January to the February after it;
    as the months' e-mails do, the newest statement is taken when a card issued twice in
    one month."""
    cycles = [f"{year}-{m:02d}" for m in range(1, 13)] + [f"{year + 1}-01", f"{year + 1}-02"]
    for cycle in cycles:
        for bill_id in latest_bills(conn, cycle).values():
            bill = load_bill(conn, bill_id)
            by_month: dict[int, list[Transaction]] = {}
            for t in bill.transactions:  # the types `report --month` takes: spending, credits
                if t.trans_date.year == year and t.txn_type in COUNTED:
                    by_month.setdefault(t.trans_date.month, []).append(t)
            for month, txns in sorted(by_month.items()):
                part = bill.model_copy(update={"transactions": txns})
                yield bill, month, txns, build_view(part, fx, rules)


def _year_spend(conn: sqlite3.Connection, year: int, fx: FxRates, rules: Rules) -> Decimal | None:
    """A year's spending, only when all its twelve months have some: a comparison with a
    year that is only partly known would mislead."""
    by_month: dict[int, Decimal] = {}
    for _, month, _, view in _pieces(conn, year, fx, rules):
        by_month[month] = by_month.get(month, ZERO) + view.spend_cny_value
    return sum(by_month.values(), ZERO) if len(by_month) == 12 else None


def build_year_report(
    conn: sqlite3.Connection,
    year: int,
    fx: FxRates,
    rules: Rules,
    *,
    now: datetime | None = None,
) -> YearReport:
    """The calendar year by transaction date, counted the way the months' e-mails count."""
    spend_by_month: dict[int, Decimal] = {}
    rebates = ZERO
    purchases = 0
    through: date | None = None
    categories: dict[str, Decimal] = {}
    merchants: dict[str, Decimal] = {}  # shop (payment markers taken off) -> CNY
    shop_category: dict[str, str] = {}  # shop -> the category its spending is counted under
    visits: dict[str, list] = {}  # shop -> [purchases, CNY]
    paid_in: dict[str, list[Decimal]] = {}  # currency paid in -> [CNY, amount in it]
    cards: dict[str, Decimal] = {}
    spent: list[tuple[int, drill.Spent]] = []  # (month, line): what the filter lists
    bought: dict[int, int] = {}  # purchases by month
    for bill, month, txns, view in _pieces(conn, year, fx, rules):
        spend_by_month[month] = spend_by_month.get(month, ZERO) + view.spend_cny_value
        cards[bill.account_id] = cards.get(bill.account_id, ZERO) + view.spend_cny_value
        for name, value in view.categories.items():
            categories[name] = categories.get(name, ZERO) + value
        for name, value in view.merchants.items():
            shop = shown_name(name)
            merchants[shop] = merchants.get(shop, ZERO) + value
        rates = _rates(bill, fx)
        spent += [(month, s) for s in drill.spent(bill, fx, rules, txns)]
        for t in txns:
            through = max(through, t.trans_date) if through else t.trans_date
            if t.currency not in rates:
                continue
            value = t.amount * rates[t.currency]
            shop = shown_name(t.merchant or t.description_raw)
            if t.txn_type == TxnType.REBATE:
                rebates -= value  # stored negative
            if t.txn_type in SPENDING_TYPES:  # as build_view files it: cash, fees too
                shop_category.setdefault(
                    shop, rules.categorize(t.description_raw, t.txn_type, t.merchant)
                )
            if t.txn_type == TxnType.PURCHASE:
                purchases += 1
                bought[month] = bought.get(month, 0) + 1
                visit = visits.setdefault(shop, [0, ZERO])
                visit[0] += 1
                visit[1] += value
                if t.orig_currency and t.orig_amount is not None:  # what the shop charged
                    currency, amount = t.orig_currency, t.orig_amount
                else:
                    currency, amount = t.currency, t.amount
                paid = paid_in.setdefault(currency, [ZERO, ZERO])
                paid[0] += value
                paid[1] += amount

    bars = [MonthBar(f"{year}-{m:02d}", spend_by_month.get(m)) for m in range(1, 13)]
    have = [b for b in bars if b.value is not None]
    if have:  # the year's biggest month is the one in the accent, with its value on it
        max(have, key=lambda b: b.value).current = True
    segments = category_segments(categories)
    tones = {s.name: s.tone for s in segments}
    explorer = build_explorer(spent, spend_by_month, bought, tones)
    return YearReport(
        year=year,
        bars=bars,
        spend=sum(spend_by_month.values(), ZERO),
        purchases=purchases,
        categories=_category_rows(categories),
        segments=segments,
        merchants=_top_merchants(merchants, shop_category, rules, tones),
        visits=_visits(visits, shop_category, tones),
        currencies=_currencies(paid_in),
        cards=_cards(cards),
        rebates=rebates,
        charges=sum((categories.get(c, ZERO) for c in CHARGES), ZERO),
        previous=_year_spend(conn, year - 1, fx, rules),
        through=through,
        complete=bool(latest_bills(conn, f"{year + 1}-01")),
        generated_at=(now or datetime.now(CHINA)).strftime("%Y-%m-%d %H:%M"),
        build=build_label(),
        explorer=explorer,
    )


def _category_rows(categories: dict[str, Decimal]) -> list[Segment]:
    """Every category of the year, largest first: the NAMED largest in their colours as in
    the donut, the rest in grey rather than folded away; 未分类 last."""
    values = {k: v for k, v in categories.items() if v > 0}
    total = sum(values.values(), ZERO)
    uncategorised = values.pop(UNCATEGORISED, None)
    ranked = sorted(values.items(), key=lambda kv: -kv[1])
    rows = [
        make_segment(name, v, total, f"s{i + 1}" if i < NAMED else "other")
        for i, (name, v) in enumerate(ranked)
    ]
    if uncategorised:
        rows.append(make_segment(UNCATEGORISED, uncategorised, total, "none"))
    return rows


def _top_merchants(
    merchants: dict[str, Decimal],
    shop_category: dict[str, str],
    rules: Rules,
    tones: dict[str, str],
) -> list[Segment]:
    """Where the money went, each shop with the emoji and colour of the category its
    purchases were counted under."""
    total = sum((v for v in merchants.values() if v > 0), ZERO)
    ranked = sorted(((k, v) for k, v in merchants.items() if v > 0), key=lambda kv: -kv[1])
    rows = []
    for name, value in ranked[:TOP_MERCHANTS]:
        category = shop_category.get(name) or rules.categorize(name, TxnType.PURCHASE)
        rows.append(
            make_segment(name, value, total, tones.get(category, "other"), emoji_for(category))
        )
    return rows


def _visits(
    visits: dict[str, list], shop_category: dict[str, str], tones: dict[str, str]
) -> list[Visit]:
    """The shops gone back to most often; a tie goes to the one that came to more."""
    ranked = sorted(visits.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
    rows = []
    for name, (count, value) in ranked[:TOP_VISITS]:
        if count < 2:  # once is not "going back"
            break
        category = shop_category.get(name, UNCATEGORISED)
        tone = tones.get(category, "other")
        rows.append(Visit(name, count, money(cents(value)), emoji_for(category), tone))
    return rows


def _currencies(paid_in: dict[str, list[Decimal]]) -> list[Segment]:
    """Purchases by the currency they were paid in, in CNY, with the amount in that
    currency under the name ("A$12,345.60"): where the money was spent, not which card."""
    values = {c: v for c, v in paid_in.items() if v[0] > 0}
    total = sum((v[0] for v in values.values()), ZERO)
    ranked = sorted(values.items(), key=lambda kv: -kv[1][0])
    return [
        Segment(
            CURRENCY_NAMES.get(code, code),
            money(cents(cny)),
            share(cny, total),
            cny,
            f"s{i + 1}" if i < NAMED else "other",
            note="" if code == "CNY" else amount_with_symbol(amount, code),
        )
        for i, (code, (cny, amount)) in enumerate(ranked)
    ]


def _cards(cards: dict[str, Decimal]) -> list[CardYear]:
    """What each card spent over the year; a card that spent nothing is left out."""
    values = {a: v for a, v in cards.items() if v > 0}
    total = sum(values.values(), ZERO)
    rows = []
    for account, value in sorted(values.items(), key=lambda kv: -kv[1]):
        label = card_label(account)
        bank, _, last4 = label.partition(" ")
        rows.append(CardYear(label, bank, last4, money(cents(value)), share(value, total)))
    return rows


# --- the filter by month and category -----------------------------------------------------


def due_year(conn: sqlite3.Connection, today: date) -> int | None:
    """The year whose review should go out now, if any: last year, once this January's
    e-mail has gone out complete (every card's January statement is in, so all of December
    is), when its review has not been sent yet, and only until the end of March. A year
    long past is never reviewed after the fact: history imported in September may hold a
    complete January, but the year before it is no year to review."""
    if (today.month, today.day) > REVIEW_UNTIL:
        return None
    january = conn.execute(
        "SELECT completed_at FROM cycle_threads WHERE cycle = ?", (f"{today.year}-01",)
    ).fetchone()
    if january is None or january["completed_at"] is None:
        return None
    year = today.year - 1
    if conn.execute("SELECT 1 FROM year_reviews WHERE year = ?", (year,)).fetchone():
        return None
    return year
