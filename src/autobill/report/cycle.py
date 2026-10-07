"""The report of a statement month: every expected card, what is owed, what was spent
and how it compares with the months before. See docs/notify.md#账单月邮件.

Every card issues one statement a month. The statement month ("账单月") is the month of
the statement date, as the banks name their statements. The month's e-mail goes out once
every card is accounted for - issued, or more than GRACE past its usual day ("可能无账单")
- and covers all of them: amount due and due date per card, due dates in order, the
categories across every card, and a summary of each new statement. Until then the month's
statements wait, so you get one e-mail a month instead of one per card (a delivered
e-mail cannot be corrected, so only the final one is worth sending).

This module gathers the figures (build_cycle_report); report/cycle_mail.py turns them into
the e-mail and report/charts.py draws its charts.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from markupsafe import Markup

from autobill import build_label
from autobill.categorize import OTHER, UNCATEGORISED, Rules
from autobill.config import PortfolioCard
from autobill.fx import FxRates
from autobill.ledger import (
    CHINA,
    SPENDING_TYPES,
    cents,
    cycle_of,
    earlier_cycles,
    month_bounds,
    previous_cycle,
    today_in_china,
)
from autobill.model import ZERO, Bill, TxnType
from autobill.report import drill
from autobill.report.charts import donut_svg, trend_svg
from autobill.report.statement import (
    DayGroup,
    StatementView,
    TxnLine,
    build_view,
    day_label,
)
from autobill.report.style import amount_with_symbol, card_label, emoji_for, money, share
from autobill.store.db import load_bill

GRACE = timedelta(days=7)  # this long past a card's usual day, it may have no statement
EXPECTED_WINDOW = timedelta(days=62)  # without a portfolio: cards with a statement this close
WEEKDAYS = "一二三四五六日"
CHIPS = {"arrived": "已出账", "pending": "待出账", "missing": "可能无账单"}
NAMED = 4  # categories named in the stacked bar; 4 + grey passes the palette validator
TREND_MONTHS = 6  # statement months in the spending trend, this one included
# A category is compared with its median over the months just before this one ("平时"),
# and noted only when it is clearly off: by both this much money and this share.
BASELINE_MONTHS = 3
NOTE_MIN_DIFF = Decimal("300")
NOTE_MIN_RATIO = Decimal("0.3")
NOTE_MAX = 3


def cycle_title(cycle: str) -> str:
    year, month = cycle.split("-")
    return f"{year}年{int(month)}月"


def _usual_date(cycle: str, day: int | None) -> date:
    """The card's usual statement date in this month; unknown: the month's last day."""
    start, end = month_bounds(cycle)
    last = end - timedelta(days=1)
    return last if day is None else start.replace(day=min(day, last.day))


def expected_cards(
    conn: sqlite3.Connection, cycle: str, portfolio: Iterable[PortfolioCard] = ()
) -> dict[str, int | None]:
    """Account -> usual statement day, for every card expected to issue this month.

    The configured portfolio if there is one, leaving out cards whose first statement is
    later than this month (they did not exist yet); otherwise every card with a statement
    within about two months of this month, at the day of its latest one. A card that did
    issue this month is always included.
    """
    start, end = month_bounds(cycle)
    cards = {p.account: p.statement_day for p in portfolio}
    for account in list(cards):
        first = conn.execute(
            "SELECT MIN(statement_date) FROM bills WHERE account_id = ?", (account,)
        ).fetchone()[0]
        if first is not None and first >= end.isoformat():
            del cards[account]
    if not portfolio:
        rows = conn.execute(
            "SELECT account_id, MAX(statement_date) FROM bills"
            " WHERE statement_date >= ? AND statement_date < ? GROUP BY account_id",
            ((start - EXPECTED_WINDOW).isoformat(), (end + EXPECTED_WINDOW).isoformat()),
        )
        cards = {r[0]: date.fromisoformat(r[1]).day for r in rows}
    for account, statement_date in conn.execute(
        "SELECT account_id, statement_date FROM bills"
        " WHERE statement_date >= ? AND statement_date < ?",
        (start.isoformat(), end.isoformat()),
    ):
        cards.setdefault(account, date.fromisoformat(statement_date).day)
    return cards


@dataclass
class CardState:
    label: str  # "农业银行 0003"
    bank: str  # "农业银行"
    last4: str  # "0003"
    state: str  # "arrived" / "pending" / "missing"
    note: str
    amount: str = ""  # what is owed, in CNY when a rate is known
    amount_orig: str = ""  # a foreign card also shows what the bank itself asks for

    @property
    def chip(self) -> str:
        return CHIPS[self.state]


@dataclass
class Segment:
    """One category of the stacked spending bar and its legend row."""

    name: str
    amount: str
    share: str
    weight: Decimal  # flex-grow: the bar is split in proportion to the amounts
    tone: str  # CSS class: s1..s4 (fixed categorical order), other, none (未分类)
    emoji: str = ""
    note: str = ""  # "比平时多 ¥800": only when this category is clearly off its usual
    lines: drill.Lines | None = None  # what the row opens to (the month's e-mail)
    folded: list[Segment] = field(default_factory=list)  # the categories in the grey row

    @property
    def label(self) -> str:
        """The grey row is "其余 3 类", not 其他: 其他 is also a category of its own (a shop
        the AI could not place), and it may be one of the three."""
        return f"其余 {len(self.folded)} 类" if self.folded else self.name


@dataclass
class CycleReport:
    cycle: str
    cards: list[CardState]
    due_total: str | None  # CNY owed across the month's cards; None: a rate is missing
    spend_total: str
    segments: list[Segment]  # the month's categories, largest first
    merchants: list[Segment]  # where the money went, across every card
    days: list[DayGroup]  # every card's transactions in one list, by date
    transaction_count: int
    generated_at: str
    build: str  # "0.2.0 (15fe0d7)": which version made this e-mail, in its footer
    spend_change: str = ""  # "比上期 +12%"; empty when last month has no statements
    trend: list[MonthBar] = field(default_factory=list)  # oldest first, this month last
    period: str = ""  # "9月13日–10月12日": the days the month's spending was made in

    def _count(self, state: str) -> int:
        return sum(1 for c in self.cards if c.state == state)

    @property
    def title(self) -> str:
        """ "2026年10月账单": the bills issued that month, not what the month spent."""
        return f"{cycle_title(self.cycle)}账单"

    @property
    def subject(self) -> str:  # the same for every e-mail of the month: one conversation
        return f"📊 {self.title}"

    @property
    def complete(self) -> bool:
        return self._count("pending") == 0

    @property
    def arrived(self) -> int:
        return self._count("arrived")

    @property
    def pending(self) -> int:
        return self._count("pending")

    @property
    def missing(self) -> int:
        return self._count("missing")

    @property
    def progress(self) -> str:
        return f"已出账 {self.arrived}/{len(self.cards)}"

    @property
    def status_line(self) -> str:
        maybe = f"，{self.missing} 张可能无账单" if self.missing else ""
        if self.complete:
            return f"本期账单已齐{maybe}"
        return f"还有 {self.pending} 张待出账{maybe}"

    @property
    def preview(self) -> str:
        """The grey line Mail shows under the subject."""
        total = f"¥{self.due_total}" if self.due_total is not None else "见邮件"
        return f"本期应还 {total} · {self.arrived} 张卡 · 消费 ¥{self.spend_total}"

    @property
    def trend_shown(self) -> bool:
        """One bar alone says nothing: the trend needs a second month with statements."""
        return sum(1 for b in self.trend if b.value is not None) >= 2

    @property
    def trend_chart(self) -> Markup:
        return trend_svg(self.trend)

    @property
    def trend_caption(self) -> str:
        """ "6 期平均 ¥14,200 · 本期比平均多 19%": the average of the months shown."""
        values = [b.value for b in self.trend if b.value is not None]
        if len(values) < 2:
            return ""
        average = sum(values, ZERO) / len(values)
        text = f"{len(values)} 期平均 ¥{average:,.0f}"
        now = self.trend[-1].value
        if average > 0 and now is not None:
            ratio = (now - average) / average
            if round(abs(ratio), 2) == 0:
                text += " · 本期和平均差不多"
            else:
                text += f" · 本期比平均{'多' if ratio > 0 else '少'} {abs(ratio):.0%}"
        return text

    @property
    def has_notes(self) -> bool:
        return any(s.note for s in self.segments)

    @property
    def category_donut(self) -> Markup:
        return donut_svg(self.segments, f"¥{self.spend_total}", "本期消费")

    @property
    def merchant_donut(self) -> Markup:
        """The top merchants as a share of everything spent, so the ring is honest about
        how much of the month these few shops are."""
        top = sum((m.weight for m in self.merchants), ZERO)
        total = sum((s.weight for s in self.segments), ZERO)
        center = share(top, total) if total > 0 else ""
        return donut_svg(self.merchants, center, f"前 {len(self.merchants)} 名占比")


@dataclass
class MonthBar:
    """One statement month of the spending trend."""

    cycle: str  # "2026-09"
    value: Decimal | None  # CNY spent, as 本期消费 counts it; None: no statements that month
    current: bool = False

    @property
    def label(self) -> str:
        return f"{int(self.cycle[5:])}月"


def make_segment(name: str, value: Decimal, total: Decimal, tone: str, emoji: str = "") -> Segment:
    return Segment(
        name, money(cents(value)), share(value, total), value, tone, emoji or emoji_for(name)
    )


def category_segments(categories: dict[str, Decimal]) -> list[Segment]:
    """The month's categories as one stacked bar: the NAMED largest in the fixed
    categorical order, the rest folded into one grey row that lists them, 未分类 last
    (grey, hatched)."""
    values = {k: v for k, v in categories.items() if v > 0}
    total = sum(values.values(), ZERO)
    if not total:
        return []
    uncategorised = values.pop(UNCATEGORISED, None)
    items = sorted(values.items(), key=lambda kv: -kv[1])
    named, rest = items[:NAMED], items[NAMED:]
    segments = [make_segment(name, v, total, f"s{i + 1}") for i, (name, v) in enumerate(named)]
    if rest:
        fold = make_segment("其他", sum((v for _, v in rest), ZERO), total, "other")
        fold.folded = [make_segment(name, v, total, "other") for name, v in rest]
        segments.append(fold)
    if uncategorised:
        segments.append(make_segment(UNCATEGORISED, uncategorised, total, "none"))
    return segments


def _top_merchants(
    merchants: dict[str, Decimal], rules: Rules, tones: dict[str, str], count: int = 5
) -> list[Segment]:
    """Where the money went across all cards, Copilot Money style: merchant, its
    category's emoji and colour, amount and share of the month."""
    total = sum((v for v in merchants.values() if v > 0), ZERO)
    top = sorted(((k, v) for k, v in merchants.items() if v > 0), key=lambda kv: -kv[1])
    rows = []
    for name, v in top[:count]:
        category = rules.categorize(name, TxnType.PURCHASE)
        tone = tones.get(category, "other")
        rows.append(make_segment(name, v, total, tone, emoji_for(category)))
    return rows


def _amount_text(bill: Bill, view: StatementView) -> str:
    if view.nothing_due:
        return "无需还款"
    if view.due_cny is not None:
        return f"¥{view.due_cny}"
    return "；".join(
        amount_with_symbol(b.amount_due, b.currency) for b in bill.balances if b.amount_due
    )


def _amount_orig(bill: Bill, view: StatementView) -> str:
    """What a foreign card itself asks for, next to the converted CNY: you repay
    the bank in that currency, so the number the bank shows has to be in the e-mail too."""
    if view.nothing_due or view.due_cny is None:  # nothing owed, or the CNY is missing and
        return ""  # _amount_text already shows the original
    owed = [b for b in bill.balances if b.amount_due and b.currency != "CNY"]
    return " + ".join(amount_with_symbol(b.amount_due, b.currency) for b in owed)


def merge_transactions(views: list[tuple[str, StatementView]]) -> tuple[list[DayGroup], int]:
    """Every card's transactions as one list by date: the month read as one statement."""
    lines = [line for _, view in views for day in view.days for line in day.lines]
    lines.sort(key=lambda t: (t.when or date.min, t.card, t.line_no))
    groups: dict[date, list[TxnLine]] = {}
    for line in lines:
        if line.when is not None:
            groups.setdefault(line.when, []).append(line)
    days = [DayGroup(day_label(when), rows) for when, rows in groups.items()]
    return days, len(lines)


@dataclass(frozen=True)
class MonthTotals:
    """What an earlier statement month spent, counted exactly as this month is."""

    spend: Decimal  # as 本期消费: refunds and rebates taken off
    categories: dict[str, Decimal]  # spending per category, as the category rows


def month_totals(
    conn: sqlite3.Connection, cycle: str, fx: FxRates, rules: Rules
) -> MonthTotals | None:
    """A statement month's spending and categories in CNY; None when that month has no
    statements at all."""
    bills = latest_bills(conn, cycle)
    if not bills:
        return None
    spend = ZERO
    categories: dict[str, Decimal] = {}
    for bill_id in bills.values():
        view = build_view(load_bill(conn, bill_id), fx, rules)
        spend += view.spend_cny_value
        for name, value in view.categories.items():
            categories[name] = categories.get(name, ZERO) + value
    return MonthTotals(spend, categories)


def month_spend_cny(
    conn: sqlite3.Connection, cycle: str, fx: FxRates, rules: Rules
) -> Decimal | None:
    """CNY spent in a statement month; None when that month has no statements at all."""
    totals = month_totals(conn, cycle, fx, rules)
    return totals.spend if totals is not None else None


def category_notes(
    segments: list[Segment], categories: dict[str, Decimal], earlier: list[MonthTotals]
) -> dict[str, str]:
    """ "比平时多 ¥800" for the named category rows that are clearly off their usual.

    Usual is the median over `earlier` (the months just before this one that have
    statements), so one month with a flight in it does not move it. At least two such
    months are needed. A row is noted when it differs from its usual by NOTE_MIN_DIFF
    and by NOTE_MIN_RATIO both, more or less alike; at most NOTE_MAX, the largest first.
    其他 and 未分类 are catch-alls and never compared.
    """
    if len(earlier) < 2:
        return {}
    found = []
    for s in segments:
        if not s.tone.startswith("s") or s.name in (OTHER, UNCATEGORISED):
            continue
        now = categories.get(s.name, ZERO)
        usual = statistics.median(m.categories.get(s.name, ZERO) for m in earlier)
        diff = now - usual
        if abs(diff) < NOTE_MIN_DIFF or (usual > 0 and abs(diff) / usual < NOTE_MIN_RATIO):
            continue
        found.append((diff, s.name))
    found.sort(key=lambda item: (-abs(item[0]), item[1]))
    return {
        name: f"比平时{'多' if diff > 0 else '少'} ¥{abs(diff):,.0f}"
        for diff, name in found[:NOTE_MAX]
    }


def _change(spend: Decimal, before: Decimal | None) -> str:
    """ "比上期 +12%", the way Screen Time compares weeks. Nothing to compare: nothing said."""
    if before is None or before <= 0 or spend <= 0:
        return ""
    ratio = (spend - before) / before
    return f"比上期 {'+' if ratio >= 0 else '-'}{abs(ratio):.0%}"


def spend_trend(
    cycle: str, history: dict[str, MonthTotals | None], spend: Decimal
) -> list[MonthBar]:
    """The last TREND_MONTHS statement months, oldest first; `spend` is this month's and
    `history` holds the months before it (month_totals)."""
    bars = [
        MonthBar(c, history[c].spend if history[c] is not None else None)
        for c in earlier_cycles(cycle, TREND_MONTHS - 1)
    ]
    return [*bars, MonthBar(cycle, spend, current=True)]


def latest_bills(conn: sqlite3.Connection, cycle: str) -> dict[str, int]:
    """Account -> the id of its statement in this month (the newest, if a card issued twice)."""
    start, end = month_bounds(cycle)
    return {
        row["account_id"]: row["id"]
        for row in conn.execute(
            "SELECT id, account_id FROM bills WHERE statement_date >= ? AND statement_date < ?"
            " ORDER BY statement_date, id",
            (start.isoformat(), end.isoformat()),
        )
    }


def statement_window(conn: sqlite3.Connection, bill: Bill) -> tuple[date, date] | None:
    """The days a statement covers: its printed period, or (BOC prints only the statement
    date) from the day after the card's statement before it, as every BOC statement on
    record runs (32 of them were checked). Without that one, the
    card's first or with a month missing, from its first purchase. None: neither."""
    if bill.period_start and bill.period_end:
        return bill.period_start, bill.period_end
    before = conn.execute(
        "SELECT MAX(statement_date) FROM bills WHERE account_id = ? AND statement_date < ?",
        (bill.account_id, bill.statement_date.isoformat()),
    ).fetchone()[0]
    if before is not None and before[:7] >= previous_cycle(cycle_of(bill.statement_date)):
        return date.fromisoformat(before) + timedelta(days=1), bill.statement_date
    days = [t.trans_date for t in bill.transactions if t.txn_type in SPENDING_TYPES]
    return (min(days), bill.statement_date) if days else None


def period_text(windows: list[tuple[date, date]]) -> str:
    """ "9月13日–10月12日": the earliest start to the latest end; "" when there is none."""
    if not windows:
        return ""
    start = min(w[0] for w in windows)
    end = max(w[1] for w in windows)
    return f"{start.month}月{start.day}日–{end.month}月{end.day}日"


def _arrived_note(bill: Bill, view: StatementView) -> str:
    parts = [f"{bill.statement_date.month}月{bill.statement_date.day}日出账"]
    if not view.nothing_due:
        due = bill.due_date
        parts.append(f"{due.month}月{due.day}日还款" if due else "还款日未知")
    if bill.status != "OK":
        parts.append(view.status[2])
    return " · ".join(parts)


def build_cycle_report(
    conn: sqlite3.Connection,
    cycle: str,
    fx: FxRates,
    rules: Rules,
    *,
    portfolio: Iterable[PortfolioCard] = (),
    today: date | None = None,
    now: datetime | None = None,
) -> CycleReport:
    """The whole statement month: every expected card, the spending across all of them,
    and their transactions in one list."""
    today = today or today_in_china()
    latest = latest_bills(conn, cycle)

    expected = expected_cards(conn, cycle, portfolio)
    cards: list[CardState] = []
    views: list[tuple[str, StatementView]] = []
    due_total: Decimal | None = ZERO
    spend = ZERO
    categories: dict[str, Decimal] = {}
    merchants: dict[str, Decimal] = {}
    spent: list[drill.Spent] = []  # what the rows open to
    windows: list[tuple[date, date]] = []  # the statements that spent something
    for account in sorted(expected, key=lambda a: (expected[a] or 32, a)):
        label = card_label(account)
        bank, _, last4 = label.partition(" ")
        day = expected[account]
        bill_id = latest.get(account)
        if bill_id is None:
            if today > _usual_date(cycle, day) + GRACE:
                when = f"通常 {day} 号出账，已过一周" if day else "这个月已经过去"
                cards.append(CardState(label, bank, last4, "missing", when))
            else:
                when = f"通常 {day} 号左右出账" if day else "本月还没出账"
                cards.append(CardState(label, bank, last4, "pending", when))
            continue

        bill = load_bill(conn, bill_id)
        view = build_view(bill, fx, rules, now)
        cards.append(
            CardState(
                label,
                bank,
                last4,
                "arrived",
                _arrived_note(bill, view),
                _amount_text(bill, view),
                _amount_orig(bill, view),
            )
        )
        views.append((label, view))
        if due_total is not None:
            due_total = None if view.due_cny_value is None else due_total + view.due_cny_value
        spend += view.spend_cny_value
        # A card with nothing spent, often one on another statement day (the BOC card on
        # the 22nd while the others moved to the 12th), would only stretch the dates.
        if view.spend_cny_value and (window := statement_window(conn, bill)):
            windows.append(window)
        for name, value in view.categories.items():
            categories[name] = categories.get(name, ZERO) + value
        for name, value in view.merchants.items():
            merchants[name] = merchants.get(name, ZERO) + value
        spent += drill.spent(bill, fx, rules)

    # Each earlier month is read once, for the trend and the category notes alike.
    history = {c: month_totals(conn, c, fx, rules) for c in earlier_cycles(cycle, TREND_MONTHS - 1)}
    segments = category_segments(categories)
    baseline = earlier_cycles(cycle, BASELINE_MONTHS)  # within the trend's months
    earlier = [m for c in baseline if (m := history[c]) is not None]
    notes = category_notes(segments, categories, earlier)
    for s in segments:
        s.note = notes.get(s.name, "")
    tones = {s.name: s.tone for s in segments}
    by_category = drill.by_category(spent)
    for s in segments:
        for row in s.folded or [s]:
            row.lines = by_category.get(row.name)
    top = _top_merchants(merchants, rules, tones)
    by_shop = drill.by_shop(spent)
    for m in top:
        m.lines = by_shop.get(m.name)
    days, count = merge_transactions(views)
    trend = spend_trend(cycle, history, spend)
    return CycleReport(
        cycle=cycle,
        cards=cards,
        due_total=money(cents(due_total)) if due_total is not None else None,
        spend_total=money(cents(spend)),
        segments=segments,
        merchants=top,
        days=days,
        transaction_count=count,
        generated_at=(now or datetime.now(CHINA)).strftime("%Y-%m-%d %H:%M"),
        build=build_label(),
        spend_change=_change(spend, trend[-2].value),
        trend=trend,
        period=period_text(windows),
    )


def cycle_complete(
    conn: sqlite3.Connection,
    cycle: str,
    portfolio: Iterable[PortfolioCard] = (),
    today: date | None = None,
) -> bool:
    """Whether every card expected this month has either issued a statement or run out of
    time (docs/notify.md#账单月邮件). The same verdict as `CycleReport.complete`, but
    without loading a single bill: the month's e-mail is only built once this is true, and
    until then it would be built and thrown away every half hour.
    """
    today = today or today_in_china()
    start, end = month_bounds(cycle)
    arrived = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT account_id FROM bills"
            " WHERE statement_date >= ? AND statement_date < ?",
            (start.isoformat(), end.isoformat()),
        )
    }
    return all(
        account in arrived or today > _usual_date(cycle, day) + GRACE
        for account, day in expected_cards(conn, cycle, portfolio).items()
    )
