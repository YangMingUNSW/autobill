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
lists put them together under the name without the markers (suggest.shown_name).

The first three cards filter the year by month and category, two groups of radio buttons
and one CSS rule per value, no script ("punched card coding"): every choice is written into
the e-mail beforehand and the chosen one shows. The bars are one set of twelve that carry
their height for every category and grow or shrink on a spring; a month chosen lists its
lines. It was tried as a prototype on iOS 27 Apple Mail.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from markupsafe import Markup

from autobill import build_label
from autobill.categorize import TYPE_CATEGORIES, UNCATEGORISED, Rules, load_rules
from autobill.fx import FxRates, RateUnavailable
from autobill.ledger import CHINA, REDUCING_TYPES, SPENDING_TYPES, cents
from autobill.model import ZERO, Bill, Transaction, TxnType
from autobill.report import drill
from autobill.report.cycle import (
    NAMED,
    MonthBar,
    Segment,
    _env,
    _segment,
    category_segments,
    donut_svg,
    latest_bills,
)
from autobill.report.statement import StatementView, build_view
from autobill.report.style import amount_with_symbol, card_label, emoji_for, money, share
from autobill.store.db import load_bill
from autobill.suggest import shown_name

TOP_MERCHANTS = 10
TOP_VISITS = 5
SHOWN_CATEGORIES = 8  # the rest fold away behind one row, as the month's transactions do
TOP_SHOPS = 5  # the shops listed for a month or a category chosen
BAR_PX = 92  # the tallest bar; every bar's height is a share of it
STRIP_PX = 28  # the tallest column of a shop's twelve months
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
class Pick:
    """The headline for one choice: a month (0: the year) and a category (0: every one)."""

    label: str  # "5月 · 旅行"
    amount: str  # "12,340.00"
    note: str  # "6 笔 · 占5月的 21%"; "没有消费"


@dataclass
class Bar:
    """One of the twelve bars: its height (0 to 1) and label for every category, as CSS
    variables --h<j> and --t<j>; the chosen category's pair is the one shown."""

    month: int
    style: str


@dataclass
class Choice:
    """A category row that chooses its category (the radio button c<j>)."""

    j: int
    segment: Segment


@dataclass
class MonthList:
    """What a month (0: the year) was spent on: rows that choose, the long tail folded."""

    month: int
    shown: list[Choice]
    folded: list[Choice]
    folded_amount: str
    folded_share: str
    donut: Markup


@dataclass
class StripBar:
    month: int
    count: int
    px: int


@dataclass
class Shop:
    """A shop in a list: it opens to its lines, and over the whole year also to its twelve
    months (visits over the columns, their height the money)."""

    name: str
    count: int
    amount: str
    emoji: str
    tone: str
    lines: drill.Lines
    strip: list[StripBar] = field(default_factory=list)


@dataclass
class Explorer:
    categories: list[str]  # the radio button c<j> is categories[j - 1]; c0 is every one
    heads: dict[tuple[int, int], Pick]  # (month, category): 0 for the year, every category
    bars: list[Bar]
    captions: list[str]  # by category
    lists: list[MonthList]  # by month, 0 the year
    shops: dict[tuple[int, int], list[Shop]]  # the year, a month, a category (not both)
    visits: list[Shop]  # the shops gone back to most often
    lines: list[tuple[int, int, drill.Line]]  # every line with its month and category j
    css: Markup  # the rules that make the choices work (explorer_css)


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
    explorer = _explorer(spent, spend_by_month, bought, tones)
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


# --- the filter by month and category -----------------------------------------------------


def _month_name(month: int) -> str:
    return "全年" if month == 0 else f"{month}月"


def _compact(value: Decimal) -> str:
    """A bar's label: "4.5万" or "7,696"; the headline has the exact figure."""
    return f"{value / 10000:.1f}万" if value >= 10000 else f"{value:,.0f}"


def _total(items: list[tuple[int, drill.Spent]]) -> Decimal:
    return sum((s.value for _, s in items), ZERO)


def _strip(items: list[tuple[int, drill.Spent]]) -> list[StripBar]:
    """A shop's twelve months: its visits over each column, the column's height its money."""
    by_month: dict[int, list[drill.Spent]] = {}
    for month, s in items:
        by_month.setdefault(month, []).append(s)
    top = max((sum((s.value for s in v), ZERO) for v in by_month.values()), default=ZERO)
    out = []
    for month in range(1, 13):
        v = sum((s.value for s in by_month.get(month, [])), ZERO)
        px = int(v / top * STRIP_PX) if top > 0 and v > 0 else 0
        out.append(StripBar(month, len(by_month.get(month, [])), max(px, 2) if v > 0 else 0))
    return out


def _shops(
    items: list[tuple[int, drill.Spent]],
    count: int,
    tones: dict[str, str],
    whole_year: dict[str, list[tuple[int, drill.Spent]]] | None = None,
) -> list[Shop]:
    """The shops that came to most, each with the emoji and colour of its main category;
    over the whole year each also carries its twelve months."""
    by_shop: dict[str, list[tuple[int, drill.Spent]]] = {}
    for month, s in items:
        by_shop.setdefault(shown_name(s.shop), []).append((month, s))
    ranked = sorted(by_shop.items(), key=lambda kv: -_total(kv[1]))[:count]
    out = []
    for name, rows in ranked:
        cats: dict[str, Decimal] = {}
        for _, s in rows:
            cats[s.category] = cats.get(s.category, ZERO) + s.value
        main = max(cats, key=lambda c: cats[c])
        shop = Shop(
            name,
            len(rows),
            money(cents(_total(rows))),
            emoji_for(main),
            tones.get(main, "other"),
            drill.lines([s for _, s in rows], by_shop=True),
        )
        if whole_year is not None:
            shop.strip = _strip(whole_year[name])
        out.append(shop)
    return out


def _month_list(
    month: int, values: list[tuple[int, str, Decimal]], tones: dict[str, str], spent: Decimal
) -> MonthList:
    """A month's categories (0: the year's), largest first, each choosing its category;
    beyond SHOWN_CATEGORIES folded under one row, unless that would fold just one. The
    donut keeps each category's colour of the year; its middle is the spending the
    headline has, refunds and cashback taken off."""
    items = sorted(((j, name, v) for j, name, v in values if v > 0), key=lambda x: -x[2])
    whole = sum((v for _, _, v in items), ZERO)
    rows = [
        Choice(j, _segment(name, v, whole, tones.get(name, "other"), emoji_for(name)))
        for j, name, v in items
    ]
    if len(rows) > SHOWN_CATEGORIES + 1:
        shown, folded = rows[:SHOWN_CATEGORIES], rows[SHOWN_CATEGORIES:]
    else:
        shown, folded = rows, []
    folded_total = sum((c.segment.weight for c in folded), ZERO)
    parts: list[Segment] = []
    rest = ZERO
    for _, name, v in items:
        tone = tones.get(name, "other")
        if tone in ("s1", "s2", "s3", "s4", "none"):
            parts.append(_segment(name, v, whole, tone))
        else:
            rest += v
    if rest > 0:
        parts.append(_segment("其余", rest, whole, "other"))
    caption = f"{_month_name(month)}消费"
    donut = donut_svg(parts, f"¥{spent:,.0f}", caption) if whole > 0 else Markup("")
    folded_share = share(folded_total, whole) if whole > 0 else ""
    return MonthList(month, shown, folded, money(cents(folded_total)), folded_share, donut)


def _explorer(
    spent: list[tuple[int, drill.Spent]],
    net: dict[int, Decimal],
    bought: dict[int, int],
    tones: dict[str, str],
) -> Explorer:
    """Every choice of month and category, worked out beforehand (docs/notify.md#年度回顾).
    Category amounts are what the categories add up (refunds and cashback not taken off);
    a whole month's, as the headline and the bars show it, has them taken off."""
    year_by_category: dict[str, Decimal] = {}
    for _, s in spent:
        year_by_category[s.category] = year_by_category.get(s.category, ZERO) + s.value
    ranked = sorted(year_by_category.items(), key=lambda kv: -kv[1])
    categories = [name for name, v in ranked if v > 0]
    index = {name: j for j, name in enumerate(categories, start=1)}
    k = len(categories)
    sub: dict[tuple[int, int], list[tuple[int, drill.Spent]]] = {}
    for month, s in spent:
        j = index.get(s.category)
        if j is None:  # a category that came to nothing over the year
            continue
        for key in ((month, j), (0, j), (month, 0), (0, 0)):
            sub.setdefault(key, []).append((month, s))

    def label(i: int, j: int) -> str:
        return _month_name(i) if j == 0 else f"{_month_name(i)} · {categories[j - 1]}"

    def month_spent(i: int) -> Decimal:
        return sum(net.values(), ZERO) if i == 0 else net.get(i, ZERO)

    heads = {}
    for i in range(13):
        for j in range(k + 1):
            if j == 0:
                value = month_spent(i)
                count = sum(bought.values()) if i == 0 else bought.get(i, 0)
                note = f"{count} 笔消费 · 已扣返现和退款"
            else:
                rows = sub.get((i, j), [])
                value, count = _total(rows), len(rows)
                whole = _total(sub.get((i, 0), []))
                part = f" · 占{_month_name(i)}的 {share(value, whole)}" if whole > 0 else ""
                note = f"{count} 笔{part}"
            heads[(i, j)] = Pick(
                label(i, j), money(cents(value)), note if value > 0 else "没有消费"
            )

    captions, heights = [], []
    for j in range(k + 1):
        values = [net.get(i, ZERO) if j == 0 else _total(sub.get((i, j), [])) for i in range(1, 13)]
        top = max(values)
        heights.append([(v / top if top > 0 and v > 0 else ZERO, v) for v in values])
        have = [(i, v) for i, v in enumerate(values, start=1) if v > 0]
        if len(have) >= 2:
            (hi, hv), (lo, lv) = max(have, key=lambda x: x[1]), min(have, key=lambda x: x[1])
            captions.append(f"最多：{hi}月 ¥{hv:,.0f} · 最少：{lo}月 ¥{lv:,.0f}")
        elif have:
            captions.append(f"只有 {have[0][0]} 月有这类消费")
        else:
            captions.append("")
    bars = []
    for i in range(12):
        parts = []
        for j in range(k + 1):
            h, v = heights[j][i]
            parts.append(f"--h{j}:{max(float(h), 0.02):.3f}")
            text = _compact(v) if v > 0 else "—"
            parts.append(f'--t{j}:"{text}"')
        bars.append(Bar(i + 1, ";".join(parts)))

    lists = []
    for i in range(13):
        values = [(j, name, _total(sub.get((i, j), []))) for j, name in enumerate(categories, 1)]
        lists.append(_month_list(i, values, tones, month_spent(i)))

    whole_year: dict[str, list[tuple[int, drill.Spent]]] = {}
    for month, s in spent:
        whole_year.setdefault(shown_name(s.shop), []).append((month, s))
    shops = {(0, 0): _shops(sub.get((0, 0), []), TOP_MERCHANTS, tones, whole_year)}
    for i in range(1, 13):  # a month's shops do not open: its lines are listed below them
        shops[(i, 0)] = _shops(sub.get((i, 0), []), TOP_SHOPS, tones)
    for j in range(1, k + 1):
        shops[(0, j)] = _shops(sub.get((0, j), []), TOP_SHOPS, tones)

    bought_at: dict[str, list[tuple[int, drill.Spent]]] = {}
    for month, s in spent:
        if s.purchase:
            bought_at.setdefault(shown_name(s.shop), []).append((month, s))
    often = sorted(bought_at.items(), key=lambda kv: (-len(kv[1]), -_total(kv[1])))
    visits = []
    for name, rows in [(n, r) for n, r in often if len(r) >= 2][:TOP_VISITS]:
        visits += _shops(rows, 1, tones, {name: rows})

    kept = [(month, s) for month, s in spent if s.category in index]
    kept.sort(key=lambda x: (x[1].day, x[1].card))
    lines = [(month, index[s.category], drill.line(s)) for month, s in kept]
    css = explorer_css(k)
    return Explorer(categories, heads, bars, captions, lists, shops, visits, lines, css)


def explorer_css(k: int) -> Markup:
    """The radio buttons m0-m12 (month; 0 the year) and c0-c<k> (category; 0 every one),
    placed before .x, choose what shows: blocks for one month (.xm), one category (.xc),
    or both (an .xm inside an .xc); the heights of the one set of bars; the lines of the
    chosen month and category. One rule per value, never per combination. What comes
    into view fades in; the bars grow and shrink on the spring (a registered --h)."""
    out = [".x .xm, .x .xc { display: none; }"]
    out += [
        f"#m{i}:checked ~ .x .xm.m{i} {{ display: block; animation: fadein .3s ease both; }}"
        for i in range(13)
    ]
    out += [
        f"#c{j}:checked ~ .x .xc.c{j} {{ display: block; animation: fadein .3s ease both; }}"
        for j in range(k + 1)
    ]
    for j in range(k + 1):
        out.append(f"#c{j}:checked ~ .x .bars .f {{ --h: var(--h{j}); }}")
        out.append(f"#c{j}:checked ~ .x .bars .v::after {{ content: var(--t{j}); }}")
    out.append("#m0:checked ~ .x .bars .f { background: var(--accent); }")
    for i in range(1, 13):
        chosen = f"#m{i}:checked ~ .x .bars label.m{i}"
        out.append(f"{chosen} .f {{ background: var(--accent); }}")
        out.append(f"{chosen} .v {{ opacity: 1; }}")
        out.append(f"{chosen} .k {{ color: var(--label); font-weight: 700; }}")
    for j in range(1, k + 1):
        out.append(
            f"#c{j}:checked ~ .x .crow.c{j} .name {{ color: var(--accent); font-weight: 600; }}"
        )
        out.append(f"#c{j}:checked ~ .x .crow.c{j} .ck {{ display: inline; }}")
    out.append("#m0:checked ~ .x .clr-m, #c0:checked ~ .x .clr-c { display: none; }")
    out.append("#m0:not(:checked) ~ .x .txsec { display: block; animation: fadein .3s ease both; }")
    out += [f"#m{i}:checked ~ .x .t:not(.m{i}) {{ display: none; }}" for i in range(1, 13)]
    out += [f"#c{j}:checked ~ .x .t:not(.c{j}) {{ display: none; }}" for j in range(1, k + 1)]
    return Markup((chr(10) + "  ").join(out))


def render_year_html(report: YearReport) -> str:
    """The e-mail, its lines stripped of the template's indentation: every choice of the
    filter is written in beforehand, so the saving adds up (about a tenth)."""
    html = _env.get_template("year_review.html.j2").render(r=report)
    return chr(10).join(line.strip() for line in html.splitlines() if line.strip())


def year_plain_text(report: YearReport) -> str:
    """For mail apps that show no HTML, and for `autobill year-review` in a terminal."""
    total = f"全年消费：¥{report.spend_total}"
    months = " · ".join(
        f"{b.label} " + (f"¥{b.value:,.0f}" if b.value is not None else "无消费")
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


def build_year_email(
    conn: sqlite3.Connection,
    year: int,
    fx: FxRates,
    sender: str,
    to_addr: str,
    *,
    rules: Rules | None = None,
) -> tuple[EmailMessage, YearReport]:
    """The year's review as an e-mail of its own: no attachment, no thread."""
    report = build_year_report(conn, year, fx, rules or load_rules(conn))
    msg = EmailMessage()
    msg["Subject"] = report.subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="autobill.invalid")
    msg["X-AutoBill-Report"] = "true"  # never parsed as a statement (fetch/mime.py)
    msg.set_content(year_plain_text(report))
    msg.add_alternative(render_year_html(report), subtype="html")
    return msg, report


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
