"""The year in review (docs/notify.md#年度回顾): the twelve statement months of a year in
one e-mail. Every statement is counted exactly as the month's e-mail counts it
(build_view), so the twelve 本月消费 add up to the year's spending. The same look as the
month's e-mail (templates/_email.css): large title, grouped lists, donuts and the monthly
columns; nothing loaded from outside, no links, no script.

Over a year one shop shows up under several names, because the bank glues its payment
markers to the name ("Woolworths OnlineAUSVISA Apple Pay", "跨行消费 ..."); the year's
lists put them together under the name without the markers (suggest.shown_name).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from markupsafe import Markup

from autobill import build_label
from autobill.categorize import TYPE_CATEGORIES, UNCATEGORISED, Rules
from autobill.fx import FxRates, RateUnavailable
from autobill.model import ZERO, Bill, TxnType
from autobill.report.cycle import (
    CHINA,
    NAMED,
    MonthBar,
    Segment,
    _env,
    _segment,
    category_segments,
    donut_svg,
    latest_bills,
    month_spend_cny,
    trend_svg,
)
from autobill.report.monthly import SPENDING_TYPES, cents
from autobill.report.statement import build_view
from autobill.report.style import amount_with_symbol, card_label, emoji_for, money, share
from autobill.store.db import load_bill
from autobill.suggest import shown_name

TOP_MERCHANTS = 10
TOP_VISITS = 5
SHOWN_CATEGORIES = 8  # the rest fold away behind one row, as the month's transactions do
CHARGES = (TYPE_CATEGORIES[TxnType.INTEREST], TYPE_CATEGORIES[TxnType.FEE])  # 利息, 手续费
CURRENCY_NAMES = {
    "CNY": "人民币", "AUD": "澳元", "USD": "美元", "EUR": "欧元", "JPY": "日元", "GBP": "英镑",
    "HKD": "港币", "MOP": "澳门元", "TWD": "新台币", "CHF": "瑞士法郎", "THB": "泰铢",
    "SGD": "新加坡元", "NZD": "新西兰元", "CAD": "加元", "KRW": "韩元", "MYR": "马来西亚林吉特",
}  # fmt: skip


@dataclass
class Visit:
    """A shop the author kept going back to: how often, and what it came to in CNY."""

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
    amount: str  # CNY spent over the year, as 本月消费 counts it
    share: str


@dataclass
class YearReport:
    year: int
    bars: list[MonthBar]  # January to December; value None: no statements that month
    spend: Decimal  # the twelve months' 本月消费 added up
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
    generated_at: str
    build: str

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
        """ "1月到12月 · 12 个账单月": which months the numbers come from."""
        have = [b for b in self.bars if b.value is not None]
        if not have:
            return "这一年还没有账单"
        return f"{have[0].label}到{have[-1].label} · {len(have)} 个账单月"

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
    def trend_shown(self) -> bool:
        return self.months >= 2

    @property
    def trend_chart(self) -> Markup:
        return trend_svg(self.bars, f"{self.year} 年每个账单月的消费")

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
    def shown_categories(self) -> list[Segment]:
        """The largest categories; folding away a single row would save nothing."""
        if len(self.categories) <= SHOWN_CATEGORIES + 1:
            return self.categories
        return self.categories[:SHOWN_CATEGORIES]

    @property
    def folded_categories(self) -> list[Segment]:
        return self.categories[len(self.shown_categories) :]

    @property
    def folded_amount(self) -> str:
        return money(cents(sum((s.weight for s in self.folded_categories), ZERO)))

    @property
    def folded_share(self) -> str:
        total = sum((s.weight for s in self.categories), ZERO)
        part = sum((s.weight for s in self.folded_categories), ZERO)
        return share(part, total) if total > 0 else ""

    @property
    def category_donut(self) -> Markup:
        return donut_svg(self.segments, f"¥{self.spend:,.0f}", "全年消费")

    @property
    def merchant_donut(self) -> Markup:
        """The top merchants as a share of everything spent, as in the month's e-mail."""
        top = sum((m.weight for m in self.merchants), ZERO)
        total = sum((s.weight for s in self.segments), ZERO)
        center = share(top, total) if total > 0 else ""
        return donut_svg(self.merchants, center, f"前 {len(self.merchants)} 名占比")

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


def _year_spend(conn: sqlite3.Connection, year: int, fx: FxRates, rules: Rules) -> Decimal | None:
    """A year's spending, only when every one of its twelve statement months has statements:
    a comparison with a year that is only partly known would mislead."""
    total = ZERO
    for month in range(1, 13):
        spend = month_spend_cny(conn, f"{year}-{month:02d}", fx, rules)
        if spend is None:
            return None
        total += spend
    return total


def build_year_report(
    conn: sqlite3.Connection,
    year: int,
    fx: FxRates,
    rules: Rules,
    *,
    now: datetime | None = None,
) -> YearReport:
    """The year's twelve statement months, added up the way the months' e-mails count them."""
    bars: list[MonthBar] = []
    spend = rebates = ZERO
    purchases = 0
    categories: dict[str, Decimal] = {}
    merchants: dict[str, Decimal] = {}  # shop (payment markers taken off) -> CNY
    shop_category: dict[str, str] = {}  # shop -> the category its spending is counted under
    visits: dict[str, list] = {}  # shop -> [purchases, CNY]
    paid_in: dict[str, list[Decimal]] = {}  # currency paid in -> [CNY, amount in it]
    cards: dict[str, Decimal] = {}
    for month in range(1, 13):
        cycle = f"{year}-{month:02d}"
        bills = latest_bills(conn, cycle)
        if not bills:
            bars.append(MonthBar(cycle, None))
            continue
        month_spend = ZERO
        for account, bill_id in bills.items():
            bill = load_bill(conn, bill_id)
            view = build_view(bill, fx, rules)
            month_spend += view.spend_cny_value
            cards[account] = cards.get(account, ZERO) + view.spend_cny_value
            for name, value in view.categories.items():
                categories[name] = categories.get(name, ZERO) + value
            for name, value in view.merchants.items():
                shop = shown_name(name)
                merchants[shop] = merchants.get(shop, ZERO) + value
            rates = _rates(bill, fx)
            for t in bill.transactions:
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
        spend += month_spend
        bars.append(MonthBar(cycle, month_spend))

    have = [b for b in bars if b.value is not None]
    if have:  # the year's biggest month is the one in the accent, with its value on it
        max(have, key=lambda b: b.value).current = True
    segments = category_segments(categories)
    tones = {s.name: s.tone for s in segments}
    return YearReport(
        year=year,
        bars=bars,
        spend=spend,
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
        generated_at=(now or datetime.now(CHINA)).strftime("%Y-%m-%d %H:%M"),
        build=build_label(),
    )


def _category_rows(categories: dict[str, Decimal]) -> list[Segment]:
    """Every category of the year, largest first: the NAMED largest in their colours as in
    the donut, the rest in grey rather than folded away; 未分类 last."""
    values = {k: v for k, v in categories.items() if v > 0}
    total = sum(values.values(), ZERO)
    uncategorised = values.pop(UNCATEGORISED, None)
    ranked = sorted(values.items(), key=lambda kv: -kv[1])
    rows = [
        _segment(name, v, total, f"s{i + 1}" if i < NAMED else "other")
        for i, (name, v) in enumerate(ranked)
    ]
    if uncategorised:
        rows.append(_segment(UNCATEGORISED, uncategorised, total, "none"))
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
        rows.append(_segment(name, value, total, tones.get(category, "other"), emoji_for(category)))
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


def render_year_html(report: YearReport) -> str:
    return _env.get_template("year_review.html.j2").render(r=report)


def year_plain_text(report: YearReport) -> str:
    """For mail apps that show no HTML, and for `autobill year-review` in a terminal."""
    total = f"全年消费：¥{report.spend_total}"
    months = " · ".join(
        f"{b.label} " + (f"¥{b.value:,.0f}" if b.value is not None else "无账单")
        for b in report.bars
    )
    out = [
        report.title,
        report.span,
        "",
        f"{total}（{report.change}）" if report.change else total,
        f"月均 {report.average} · {report.purchases} 笔消费",
        f"每个月：{months}",
    ]
    if report.categories:
        out += ["", "分类："]
        out += [f"{s.name} {s.share} ¥{s.amount}" for s in report.categories]
    if report.merchants:
        out += ["", "花得最多的商户："]
        out += [f"{i}. {m.name} ¥{m.amount}" for i, m in enumerate(report.merchants, 1)]
    if report.visits:
        out += ["", "去得最多的店："]
        out += [f"{v.name} {v.count} 次 ¥{v.amount}" for v in report.visits]
    if report.currencies:
        out += ["", "用哪些货币消费："]
        out += [
            f"{c.name} {c.share} ¥{c.amount}" + (f"（{c.note}）" if c.note else "")
            for c in report.currencies
        ]
    if report.cards:
        out += ["", "各张卡："]
        out += [f"{c.label} ¥{c.amount}（{c.share}）" for c in report.cards]
    out += [
        "",
        f"返现 +¥{report.rebates_text} · 利息和手续费 ¥{report.charges_text}",
        f"生成于 {report.generated_at} · 版本 {report.build}",
    ]
    return "\n".join(out)
