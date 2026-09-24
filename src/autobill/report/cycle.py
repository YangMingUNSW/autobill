"""The e-mail of a statement month, sent once the month is complete.
See docs/notify.md#账单月邮件.

Every card issues one statement a month. The statement month ("账单月") is the month of
the statement date, as the banks name their statements. The month's e-mail goes out once
every card is accounted for - issued, or more than GRACE past its usual day ("可能无账单")
- and covers all of them: amount due and due date per card, due dates in order, the
categories across every card, and a summary of each new statement. Until then the month's
statements wait, so the author gets one e-mail a month instead of one per card
(2026-09-20; a delivered e-mail cannot be corrected, so only the final one is worth
sending).

A month that gets a second e-mail - a statement arriving late, or `autobill resend` after
a fix - shares the first one's subject and points at it with In-Reply-To and References,
so Apple Mail shows them as one conversation. Every e-mail is rebuilt from the database,
so the newest one is always the whole month as it stands now. The layout is written for Apple
Mail on iPhone (WebKit): <style>, CSS variables, dark mode and inline SVG work there.
Nothing is loaded from outside and there are no links. Due dates are shown, never
reminders.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup, escape

from autobill.categorize import UNCATEGORISED, Rules, load_rules
from autobill.config import PortfolioCard
from autobill.fx import FxRates
from autobill.model import ZERO, Bill, TxnType
from autobill.report.monthly import cents, month_bounds
from autobill.report.statement import (
    DayGroup,
    StatementView,
    TxnLine,
    build_view,
    day_label,
)
from autobill.report.style import amount_with_symbol, card_label, emoji_for, money, share
from autobill.store.db import load_bill
from autobill.store.db import now as db_now

CHINA = timezone(timedelta(hours=8))  # statement and due dates are Beijing time (no DST)
GRACE = timedelta(days=7)  # this long past a card's usual day, it may have no statement
EXPECTED_WINDOW = timedelta(days=62)  # without a portfolio: cards with a statement this close
WEEKDAYS = "一二三四五六日"
CHIPS = {"arrived": "已出账", "pending": "待出账", "missing": "可能无账单"}
NAMED = 4  # categories named in the stacked bar; 4 + grey passes the palette validator
TREND_MONTHS = 6  # statement months in the spending trend, this one included


def cycle_of(statement_date: date) -> str:
    return statement_date.strftime("%Y-%m")


def cycle_title(cycle: str) -> str:
    year, month = cycle.split("-")
    return f"{year}年{int(month)}月"


def today_in_china() -> date:
    return datetime.now(CHINA).date()


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
    spend_change: str = ""  # "比 9 月 +12%"; empty when last month has no statements
    trend: list[MonthBar] = field(default_factory=list)  # oldest first, this month last

    def _count(self, state: str) -> int:
        return sum(1 for c in self.cards if c.state == state)

    @property
    def title(self) -> str:
        return cycle_title(self.cycle)

    @property
    def subject(self) -> str:  # the same for every e-mail of the month: one conversation
        return f"📊 {self.title} 信用卡账单"

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
            return f"本月账单已齐{maybe}"
        return f"还有 {self.pending} 张待出账{maybe}"

    @property
    def preview(self) -> str:
        """The grey line Mail shows under the subject."""
        total = f"¥{self.due_total}" if self.due_total is not None else "见邮件"
        return f"本月合计应还 {total} · {self.arrived} 张卡 · 消费 ¥{self.spend_total}"

    @property
    def trend_shown(self) -> bool:
        """One bar alone says nothing: the trend needs a second month with statements."""
        return sum(1 for b in self.trend if b.value is not None) >= 2

    @property
    def trend_chart(self) -> Markup:
        return trend_svg(self.trend)

    @property
    def trend_caption(self) -> str:
        """ "6 个月平均 ¥14,200 · 本月比平均多 19%": the average of the months shown."""
        values = [b.value for b in self.trend if b.value is not None]
        if len(values) < 2:
            return ""
        average = sum(values, ZERO) / len(values)
        text = f"{len(values)} 个月平均 ¥{average:,.0f}"
        now = self.trend[-1].value
        if average > 0 and now is not None:
            ratio = (now - average) / average
            if round(abs(ratio), 2) == 0:
                text += " · 本月和平均差不多"
            else:
                text += f" · 本月比平均{'多' if ratio > 0 else '少'} {abs(ratio):.0%}"
        return text

    @property
    def category_donut(self) -> Markup:
        return donut_svg(self.segments, f"¥{self.spend_total}", "本月消费")

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
    value: Decimal | None  # CNY spent, as 本月消费 counts it; None: no statements that month
    current: bool = False

    @property
    def label(self) -> str:
        return f"{int(self.cycle[5:])}月"


def trend_svg(bars: list[MonthBar]) -> Markup:
    """One column per statement month, Apple Card's monthly activity: this month in the
    accent, the months before in grey (emphasis, not categories). Marks follow the dataviz
    spec: 4px rounded top, square base, one hairline baseline, only this month's value
    written on its column; every month's value is in the aria-label and the plain-text
    part. A month without statements is a dash, so the months stay evenly spaced."""
    if not bars:
        return Markup("")
    # viewBox units: text is sized ~20 so it is ~11px when a phone scales 600 to ~330.
    width, height, top_pad, bottom_pad = 600, 210, 40, 34
    plot_h = height - top_pad - bottom_pad
    base = height - bottom_pad
    slot = width / len(bars)
    bar_w = 40.0  # ~22px on a phone: under the 24px cap
    peak = max((b.value for b in bars if b.value is not None and b.value > 0), default=ZERO)
    said = "，".join(
        f"{b.label} " + (f"¥{b.value:,.0f}" if b.value is not None else "无账单") for b in bars
    )
    parts = [
        f'<svg class="trend" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape("近 " + str(len(bars)) + " 个月消费（人民币）：" + said)}">',
        f'<line class="axis" x1="0" y1="{base}" x2="{width}" y2="{base}" />',
    ]
    for i, bar in enumerate(bars):
        cx = i * slot + slot / 2
        now = " now" if bar.current else ""
        parts.append(
            f'<text class="tick{now}" x="{cx:.1f}" y="{height - 6}" text-anchor="middle">'
            f"{bar.label}</text>"
        )
        if bar.value is None:
            parts.append(
                f'<text class="gap" x="{cx:.1f}" y="{base - 8}" text-anchor="middle">—</text>'
            )
            continue
        top = base
        if bar.value > 0 and peak > 0:
            h = max(float(bar.value / peak) * plot_h, 2.0)
            x, top = cx - bar_w / 2, base - h
            r = min(4.0, bar_w / 2, h)
            parts.append(
                f'<path class="bar{now}" d="M{x:.1f},{base} V{top + r:.1f} '
                f"Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} H{x + bar_w - r:.1f} "
                f'Q{x + bar_w:.1f},{top:.1f} {x + bar_w:.1f},{top + r:.1f} V{base} Z" />'
            )
        if bar.current:
            parts.append(
                f'<text class="value" x="{cx:.1f}" y="{top - 8:.1f}" text-anchor="middle">'
                f"¥{bar.value:,.0f}</text>"
            )
    parts.append("</svg>")
    return Markup("".join(parts))


def donut_svg(segments: list[Segment], center: str, caption: str) -> Markup:
    """The shares as a ring, one arc per segment, clockwise from 12 o'clock in the order of
    the legend below it. Colours come from CSS classes, so the chart follows light and dark
    mode; a 2px gap keeps neighbouring slices apart. Every slice is named with its share in
    the legend, so a slice too thin to see is never the only place a number appears.
    Merchant names come from the statement, so every text is escaped ("H&M", a quote).
    """
    total = sum((s.weight for s in segments), ZERO)
    if not segments or total <= 0:
        return Markup("")
    size, stroke = 180, 30
    r = (size - stroke) / 2
    c = 2 * math.pi * r
    gap = 2.0
    said = "，".join(f"{s.name} {s.share}" for s in segments)
    parts = [
        f'<svg class="donut" viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'role="img" aria-label="{escape(caption)}：{escape(said)}">',
        f'<circle class="track" cx="{size / 2}" cy="{size / 2}" r="{r}" />',
        f'<g transform="rotate(-90 {size / 2} {size / 2})">',
    ]
    offset = 0.0
    for segment in segments:
        length = float(segment.weight / total) * c
        dash = max(length - gap, 1.0)
        parts.append(
            f'<circle class="slice {segment.tone}" cx="{size / 2}" cy="{size / 2}" r="{r}" '
            f'stroke-dasharray="{dash:.2f} {c - dash:.2f}" stroke-dashoffset="{-offset:.2f}" />'
        )
        offset += length
    parts.append("</g>")
    if center:
        parts.append(
            f'<text class="donut-num" x="50%" y="49%" text-anchor="middle">{escape(center)}</text>'
            f'<text class="donut-cap" x="50%" y="63%" text-anchor="middle">{escape(caption)}</text>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def _segment(name: str, value: Decimal, total: Decimal, tone: str, emoji: str = "") -> Segment:
    return Segment(
        name, money(cents(value)), share(value, total), value, tone, emoji or emoji_for(name)
    )


def category_segments(categories: dict[str, Decimal]) -> list[Segment]:
    """The month's categories as one stacked bar: the NAMED largest in the fixed
    categorical order, the rest folded into 其他 (grey), 未分类 last (grey, hatched)."""
    values = {k: v for k, v in categories.items() if v > 0}
    total = sum(values.values(), ZERO)
    if not total:
        return []
    uncategorised = values.pop(UNCATEGORISED, None)
    items = sorted(values.items(), key=lambda kv: -kv[1])
    named, rest = items[:NAMED], items[NAMED:]
    segments = [_segment(name, v, total, f"s{i + 1}") for i, (name, v) in enumerate(named)]
    if rest:
        segments.append(_segment("其他", sum((v for _, v in rest), ZERO), total, "other"))
    if uncategorised:
        segments.append(_segment(UNCATEGORISED, uncategorised, total, "none"))
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
        rows.append(_segment(name, v, total, tone, emoji_for(category)))
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
    """What a foreign card itself asks for, next to the converted CNY: the author repays
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


def month_spend_cny(
    conn: sqlite3.Connection, cycle: str, fx: FxRates, rules: Rules
) -> Decimal | None:
    """CNY spent in a statement month, for the month-on-month line; None when that month
    has no statements at all."""
    bills = latest_bills(conn, cycle)
    if not bills:
        return None
    total = ZERO
    for bill_id in bills.values():
        total += build_view(load_bill(conn, bill_id), fx, rules).spend_cny_value
    return total


def _change(spend: Decimal, before: Decimal | None, cycle: str) -> str:
    """ "比 9 月 +12%", the way Screen Time compares weeks. Nothing to compare: nothing said."""
    if before is None or before <= 0 or spend <= 0:
        return ""
    ratio = (spend - before) / before
    return f"比 {cycle_title(cycle)} {'+' if ratio >= 0 else '-'}{abs(ratio):.0%}"


def previous_cycle(cycle: str) -> str:
    start, _ = month_bounds(cycle)
    return (start - timedelta(days=1)).strftime("%Y-%m")


def spend_trend(
    conn: sqlite3.Connection, cycle: str, fx: FxRates, rules: Rules, spend: Decimal
) -> list[MonthBar]:
    """The last TREND_MONTHS statement months, oldest first; `spend` is this month's."""
    cycles = [cycle]
    while len(cycles) < TREND_MONTHS:
        cycles.insert(0, previous_cycle(cycles[0]))
    bars = [MonthBar(c, month_spend_cny(conn, c, fx, rules)) for c in cycles[:-1]]
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
        for name, value in view.categories.items():
            categories[name] = categories.get(name, ZERO) + value
        for name, value in view.merchants.items():
            merchants[name] = merchants.get(name, ZERO) + value

    segments = category_segments(categories)
    tones = {s.name: s.tone for s in segments}
    days, count = merge_transactions(views)
    before = previous_cycle(cycle)
    trend = spend_trend(conn, cycle, fx, rules, spend)
    return CycleReport(
        cycle=cycle,
        cards=cards,
        due_total=money(cents(due_total)) if due_total is not None else None,
        spend_total=money(cents(spend)),
        segments=segments,
        merchants=_top_merchants(merchants, rules, tones),
        days=days,
        transaction_count=count,
        generated_at=(now or datetime.now(CHINA)).strftime("%Y-%m-%d %H:%M"),
        spend_change=_change(spend, trend[-2].value, before),
        trend=trend,
    )


def _yuan(amount: str) -> Markup:
    """ "16,714.84" -> ¥16,714 with smaller .84, as Apple Card and Wallet show money."""
    whole, _, fraction = amount.partition(".")
    return Markup('<span class="cur">¥</span>{}<span class="dec">.{}</span>').format(
        whole, fraction or "00"
    )


_env = Environment(
    loader=PackageLoader("autobill.report", "templates"),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["yuan"] = _yuan
_env.filters["emoji"] = emoji_for


def render_cycle_html(report: CycleReport) -> str:
    return _env.get_template("cycle_report.html.j2").render(r=report)


def plain_text(report: CycleReport) -> str:
    """For mail apps that show no HTML."""
    out = [f"{report.title} 信用卡账单", report.status_line]
    if report.due_total is not None:
        out.append(f"本月合计应还：¥{report.due_total}")
    spent = f"本月消费：¥{report.spend_total}"
    out.append(f"{spent}（{report.spend_change}）" if report.spend_change else spent)
    if report.trend_shown:
        months = " · ".join(
            f"{b.label} " + (f"¥{b.value:,.0f}" if b.value is not None else "无账单")
            for b in report.trend
        )
        out.append(f"近 {len(report.trend)} 个月：{months}")
    out.append("")
    for card in report.cards:
        line = f"{card.label}：{card.chip} {card.amount}".rstrip()
        if card.amount_orig:
            line += f"（{card.amount_orig}）"
        out.append(f"{line}（{card.note}）")
    if report.segments:
        out += ["", "分类："]
        out += [f"{s.name} {s.share} ¥{s.amount}" for s in report.segments]
    out += ["", f"全部 {report.transaction_count} 笔流水见 HTML 版本。"]
    return "\n".join(out)


def thread_ids(conn: sqlite3.Connection, cycle: str) -> list[str]:
    row = conn.execute("SELECT message_ids FROM cycle_threads WHERE cycle = ?", (cycle,)).fetchone()
    return json.loads(row[0]) if row else []


def record_sent(conn: sqlite3.Connection, cycle: str, message_id: str, complete: bool) -> None:
    ids = [*thread_ids(conn, cycle), message_id]
    stamp = db_now()
    conn.execute(
        "INSERT INTO cycle_threads (cycle, message_ids, completed_at, updated_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (cycle) DO UPDATE SET"
        " message_ids = excluded.message_ids,"
        " completed_at = excluded.completed_at,"
        " updated_at = excluded.updated_at",
        (cycle, json.dumps(ids), stamp if complete else None, stamp),
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


def build_cycle_email(
    conn: sqlite3.Connection,
    cycle: str,
    fx: FxRates,
    sender: str,
    to_addr: str,
    *,
    portfolio: Iterable[PortfolioCard] = (),
    rules: Rules | None = None,
    today: date | None = None,
) -> tuple[EmailMessage, CycleReport]:
    """The month's e-mail: the whole statement month as it stands now."""
    rules = rules or load_rules(conn)
    report = build_cycle_report(conn, cycle, fx, rules, portfolio=portfolio, today=today)
    msg = EmailMessage()
    msg["Subject"] = report.subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="autobill.invalid")
    previous = thread_ids(conn, cycle)
    if previous:  # Apple Mail threads on these headers, not only on the subject
        msg["In-Reply-To"] = previous[-1]
        msg["References"] = " ".join(previous)
    msg["X-AutoBill-Report"] = "true"  # second guard against ever parsing our own reports
    msg.set_content(plain_text(report))
    msg.add_alternative(render_cycle_html(report), subtype="html")
    return msg, report


def preview_cycle_html(
    conn: sqlite3.Connection,
    cycle: str,
    fx: FxRates,
    *,
    portfolio: Iterable[PortfolioCard] = (),
    rules: Rules | None = None,
    today: date | None = None,
) -> str:
    """The HTML of the month's e-mail, exactly as it would be sent. Sends nothing."""
    report = build_cycle_report(
        conn, cycle, fx, rules or load_rules(conn), portfolio=portfolio, today=today
    )
    return render_cycle_html(report)
