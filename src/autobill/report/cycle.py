"""The progress e-mail of a statement month. See docs/notify.md#账单月进度邮件.

Every card issues one statement a month. The statement month ("账单月") is the month of
the statement date, as the banks name their statements. Each run that imports new
statements sends one e-mail per statement month: which cards have issued (amount due, due
date), which are still to come, and a summary of each new statement with the full standard
statement attached as a PDF. Once every card is accounted for - issued, or more than GRACE
past its usual day ("可能无账单") - the e-mail is the month's final one: due dates in
order and the categories across all cards.

All e-mails of one month share a subject and point at each other with In-Reply-To and
References, so Apple Mail shows them as one conversation. The layout is written for Apple
Mail on iPhone (WebKit): <style>, CSS variables, dark mode and inline SVG work there.
Nothing is loaded from outside and there are no links. Due dates are shown, never
reminders.
"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup

from autobill.categorize import UNCATEGORISED, Rules, load_rules
from autobill.config import PortfolioCard
from autobill.fx import FxRates
from autobill.model import ZERO, Bill, TxnType
from autobill.report.monthly import cents, month_bounds
from autobill.report.pdf import PdfError, html_to_pdf
from autobill.report.statement import (
    StatementView,
    build_view,
    daily_svg,
    render_statement_html,
)
from autobill.report.style import card_label, emoji_for, money, share
from autobill.store.db import load_bill
from autobill.store.db import now as db_now

CHINA = timezone(timedelta(hours=8))  # statement and due dates are Beijing time (no DST)
GRACE = timedelta(days=7)  # this long past a card's usual day, it may have no statement
EXPECTED_WINDOW = timedelta(days=62)  # without a portfolio: cards with a statement this close
WEEKDAYS = "一二三四五六日"
CHIPS = {"arrived": "已出账", "pending": "待出账", "missing": "可能无账单"}
NAMED = 4  # categories named in the stacked bar; 4 + grey passes the palette validator

Attacher = Callable[[Bill], "bytes | None"]


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
    amount: str = ""
    is_new: bool = False

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
class NewBill:
    label: str
    view: StatementView
    chart: Markup  # daily bars with the average line
    average: str  # the dashed line's value, named in the caption
    top: list[Segment]  # the bill's largest categories, in the month's colours
    attachment: str = ""  # file name of the attached PDF; "" when there is none


@dataclass
class DueLine:
    month: str  # "9月"
    day: int
    weekday: str  # "周日"
    label: str
    amount: str


@dataclass
class CycleReport:
    cycle: str
    cards: list[CardState]
    new_bills: list[NewBill]
    due_total: str | None  # CNY owed on the statements issued so far; None: a rate is missing
    due_lines: list[DueLine]
    spend_total: str
    segments: list[Segment]
    merchants: list[Segment]
    generated_at: str

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
        total = f"¥ {self.due_total}" if self.due_total is not None else "见邮件"
        return f"{self.progress} · 合计应还 {total} · {self.status_line}"

    @property
    def ring(self) -> Markup:
        return progress_ring([c.state for c in self.cards], f"{self.arrived}/{len(self.cards)}")


def progress_ring(states: list[str], center: str) -> Markup:
    """An Activity-ring style progress: one arc per card, clockwise from 12 o'clock, in
    the order of the card list. Colours come from CSS classes (light and dark mode)."""
    size, stroke = 96, 10
    r = (size - stroke) / 2
    c = 2 * math.pi * r
    n = max(len(states), 1)
    step = c / n
    gap = 5.0  # visible gap between arcs, on top of the round caps
    dash = step - gap - stroke
    parts = [
        f'<svg class="ring" viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'role="img" aria-label="已出账 {center}">',
        f'<g transform="rotate(-90 {size / 2} {size / 2})">',
    ]
    if dash < 2:  # too many cards for separate arcs: one arc for the share issued
        done = sum(1 for s in states if s == "arrived") / n
        parts.append(f'<circle class="track" cx="{size / 2}" cy="{size / 2}" r="{r}" />')
        if done:
            parts.append(
                f'<circle class="arc arrived" cx="{size / 2}" cy="{size / 2}" r="{r}" '
                f'stroke-dasharray="{c * done:.2f} {c:.2f}" />'
            )
    else:
        for i, state in enumerate(states):
            offset = -(i * step + (gap + stroke) / 2)
            parts.append(
                f'<circle class="arc {state}" cx="{size / 2}" cy="{size / 2}" r="{r}" '
                f'stroke-dasharray="{dash:.2f} {c - dash:.2f}" stroke-dashoffset="{offset:.2f}" />'
            )
    parts.append("</g>")
    parts.append(f'<text class="ring-num" x="50%" y="47%" text-anchor="middle">{center}</text>')
    parts.append('<text class="ring-cap" x="50%" y="66%" text-anchor="middle">已出账</text>')
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


def _top_of_bill(view: StatementView, tones: dict[str, str], count: int = 3) -> list[Segment]:
    """A bill's largest categories, coloured as in the month's bar so a colour always
    means the same category within one e-mail."""
    values = {k: v for k, v in view.categories.items() if v > 0}
    total = sum(values.values(), ZERO)
    top = sorted(values.items(), key=lambda kv: -kv[1])[:count]
    return [_segment(name, v, total, tones.get(name, "other")) for name, v in top]


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


def _daily_average(view: StatementView) -> str:
    days = len(view.chart_days)
    total = sum(view.daily_values.values(), ZERO)
    return f"¥{total / days:,.0f}" if days and total > 0 else ""


def _amount_text(bill: Bill, view: StatementView) -> str:
    if view.nothing_due:
        return "无需还款"
    if view.due_cny is not None:
        return f"¥{view.due_cny}"
    return "；".join(f"{b.currency} {money(b.amount_due)}" for b in bill.balances if b.amount_due)


def _arrived_note(bill: Bill, view: StatementView) -> str:
    parts = [f"{bill.statement_date.month}月{bill.statement_date.day}日出账"]
    if not view.nothing_due:
        due = bill.due_date
        parts.append(f"{due.month}月{due.day}日前还款" if due else "还款日未知")
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
    new_bill_ids: Iterable[int] = (),
    attachments: dict[int, str] | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> CycleReport:
    today = today or today_in_china()
    new_ids = set(new_bill_ids)
    attachments = attachments or {}
    start, end = month_bounds(cycle)
    latest: dict[str, int] = {}
    for row in conn.execute(
        "SELECT id, account_id FROM bills WHERE statement_date >= ? AND statement_date < ?"
        " ORDER BY statement_date, id",
        (start.isoformat(), end.isoformat()),
    ):
        latest[row["account_id"]] = row["id"]

    expected = expected_cards(conn, cycle, portfolio)
    cards: list[CardState] = []
    new_views: list[tuple[str, StatementView, str]] = []
    due_lines: list[tuple[date, DueLine]] = []
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
        amount = _amount_text(bill, view)
        note = _arrived_note(bill, view)
        cards.append(CardState(label, bank, last4, "arrived", note, amount, bill_id in new_ids))
        if bill_id in new_ids:
            new_views.append((label, view, attachments.get(bill_id, "")))
        if due_total is not None:
            due_total = None if view.due_cny_value is None else due_total + view.due_cny_value
        spend += view.spend_cny_value
        for name, value in view.categories.items():
            categories[name] = categories.get(name, ZERO) + value
        for name, value in view.merchants.items():
            merchants[name] = merchants.get(name, ZERO) + value
        if not view.nothing_due and bill.due_date:
            d = bill.due_date
            line = DueLine(f"{d.month}月", d.day, f"周{WEEKDAYS[d.weekday()]}", label, amount)
            due_lines.append((d, line))

    segments = category_segments(categories)
    tones = {s.name: s.tone for s in segments}
    new_bills = [
        NewBill(
            label,
            view,
            daily_svg(view.chart_days, view.daily_values, average=True),
            _daily_average(view),
            _top_of_bill(view, tones),
            attachment,
        )
        for label, view, attachment in new_views
    ]
    return CycleReport(
        cycle=cycle,
        cards=cards,
        new_bills=new_bills,
        due_total=money(cents(due_total)) if due_total is not None else None,
        due_lines=[line for _, line in sorted(due_lines, key=lambda x: x[0])],
        spend_total=money(cents(spend)),
        segments=segments,
        merchants=_top_merchants(merchants, rules, tones),
        generated_at=(now or datetime.now(CHINA)).strftime("%Y-%m-%d %H:%M"),
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
    out = [f"{report.title} 信用卡账单", f"{report.progress} · {report.status_line}"]
    if report.due_total is not None:
        out.append(f"已出账合计应还：¥ {report.due_total}")
    out.append("")
    for card in report.cards:
        out.append(f"{card.label}：{card.chip} {card.amount}".rstrip() + f"（{card.note}）")
    for new in report.new_bills:
        out += ["", f"新到账单：{new.label}，本期消费 ¥ {new.view.spend_cny}"]
        if new.attachment:
            out.append(f"完整账单见附件 {new.attachment}")
    if report.complete and report.due_lines:
        out += ["", "还款日一览："]
        out += [f"{d.month}{d.day}日 {d.weekday} {d.label} {d.amount}" for d in report.due_lines]
    return "\n".join(out)


def attachment_name(bill: Bill) -> str:
    return f"AutoBill-{bill.account_id.replace(':', '-')}-{bill.statement_date}.pdf"


def pdf_attacher(fx: FxRates, rules: Rules, browser: Path) -> Attacher:
    """Prints each new bill's standard statement to PDF; None when printing fails."""

    def attach(bill: Bill) -> bytes | None:
        with tempfile.TemporaryDirectory(
            prefix="autobill-mail-", ignore_cleanup_errors=True
        ) as tmp:
            html, pdf = Path(tmp) / "statement.html", Path(tmp) / "statement.pdf"
            html.write_text(render_statement_html(bill, fx, rules), encoding="utf-8")
            try:
                html_to_pdf(html, pdf, browser)
            except PdfError:
                return None
            return pdf.read_bytes()

    return attach


# --- threads: all e-mails of one month form one conversation -------------------------


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


def open_cycles(conn: sqlite3.Connection) -> list[str]:
    """Months that got a progress e-mail but no final one yet."""
    rows = conn.execute("SELECT cycle FROM cycle_threads WHERE completed_at IS NULL ORDER BY cycle")
    return [r[0] for r in rows]


def build_cycle_email(
    conn: sqlite3.Connection,
    cycle: str,
    new_bill_ids: list[int],
    fx: FxRates,
    sender: str,
    to_addr: str,
    *,
    portfolio: Iterable[PortfolioCard] = (),
    rules: Rules | None = None,
    attach: Attacher | None = None,
    today: date | None = None,
) -> tuple[EmailMessage, CycleReport]:
    """The progress e-mail for one month, with the new statements attached as PDF."""
    rules = rules or load_rules()
    pdfs: dict[int, tuple[str, bytes]] = {}
    for bill_id in new_bill_ids:
        bill = load_bill(conn, bill_id)
        data = attach(bill) if attach else None
        if data:
            pdfs[bill_id] = (attachment_name(bill), data)
    report = build_cycle_report(
        conn,
        cycle,
        fx,
        rules,
        portfolio=portfolio,
        new_bill_ids=new_bill_ids,
        attachments={k: name for k, (name, _) in pdfs.items()},
        today=today,
    )
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
    for name, data in pdfs.values():
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
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
    """The HTML of the month's next e-mail, as if the unreported statements (or else the
    latest one) had just arrived, each with its PDF attached. Sends nothing."""
    start, end = month_bounds(cycle)
    rows = conn.execute(
        "SELECT id, account_id, statement_date, reported_at FROM bills"
        " WHERE statement_date >= ? AND statement_date < ? ORDER BY statement_date, id",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    new = [r for r in rows if r["reported_at"] is None] or rows[-1:]
    names = {
        r["id"]: f"AutoBill-{r['account_id'].replace(':', '-')}-{r['statement_date']}.pdf"
        for r in new
    }
    report = build_cycle_report(
        conn,
        cycle,
        fx,
        rules or load_rules(),
        portfolio=portfolio,
        new_bill_ids=list(names),
        attachments=names,
        today=today,
    )
    return render_cycle_html(report)
