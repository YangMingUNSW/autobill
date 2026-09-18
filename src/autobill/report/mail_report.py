"""The report e-mail sent for each imported bill. See docs/notify.md#邮件内容.

Part 1 summarises the bill; part 2 has one section per month the bill touches, with the
latest totals for that month across all cards. Only totals, never single transactions; no
links; no payment reminders (due dates are shown, nothing more).

Layout is MJML (mjml-python), which turns it into table-based HTML that mail clients
render reliably. Every chart is an HTML bar made of table cells, not a picture: QQ Mail
and NetEase Mail hide pictures by default, and the report must be complete without them.
The same HTML is written to a local preview file, so the layout can be checked before any
mail is sent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from jinja2 import Environment, PackageLoader, select_autoescape
from mjml import mjml_to_html

from autobill.categorize import UNCATEGORISED, Rules, load_rules
from autobill.fx import FxRates, RateUnavailable
from autobill.model import ZERO, Bill
from autobill.report.monthly import (
    CREDIT_NAMES,
    REDUCING_TYPES,
    SPENDING_TYPES,
    MonthlySummary,
    cents,
    month_bounds,
    monthly_summary,
    render_text,
)
from autobill.store.db import load_bill

BANK_NAMES = {"ABC": "农业银行", "CCB": "建设银行", "BOC": "中国银行"}
STATUS_NOTES = {"WARN": "这份账单对账有警告，请以银行原账单为准", "UNVERIFIED": "这份账单未对账"}
EXPECTED_WINDOW = timedelta(days=62)  # a card with a bill this close to the month is expected
NEXT_STATEMENT = timedelta(days=45)  # the statement covering a month end closes within this
MIN_BAR = 2  # percent: even the smallest positive value gets a visible bar
TOP_CATEGORIES = 6  # more categories than this are folded into "其他"
TOP_UNCATEGORISED_SHOWN = 5

# Colours: one accent for what matters, grey for context; text never wears series colour.
# Accent is slot 1 of the validated reference palette (dataviz skill, references/palette.md).
COLORS = {
    "page": "#f2f2f0",
    "card": "#ffffff",
    "text": "#0b0b0b",
    "muted": "#52514e",
    "accent": "#2a78d6",
    "context": "#c9c8c3",
    "warn_bg": "#fdf1dc",
    "warn_text": "#6b4a00",
}


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def card_label(account_id: str) -> str:
    bank, _, card = account_id.partition(":")
    name = BANK_NAMES.get(bank, bank)
    return f"{name} {'未知卡号' if card == 'unknown' else card}"


@dataclass
class Row:
    """One line of a bar list: label, amount, an optional note and a bar length (percent)."""

    name: str
    amount: str
    note: str = ""
    width: int = 0  # 0 = no bar
    color: str = ""  # "" = the list's colour


def bar_rows(items: list[tuple[str, Decimal]], total: Decimal | None = None) -> list[Row]:
    """Ranked bars scaled to the largest value; with `total`, a share note is added."""
    top = max((v for _, v in items if v > 0), default=ZERO)
    rows = []
    for name, value in items:
        width = max(int(value / top * 100), MIN_BAR) if top and value > 0 else 0
        note = share(value, total) if total and value > 0 else ""
        rows.append(Row(name, money(value), note, width))
    return rows


def share(value: Decimal, total: Decimal) -> str:
    """ "57%"; a small but non-zero share reads "<1%", never "0%"."""
    ratio = value / total
    return "<1%" if ratio < Decimal("0.005") else f"{ratio:.0%}"


@dataclass
class BillDigest:
    bank_name: str
    cards_text: str
    statement_date: date
    due_date: date | None
    status_note: str
    amounts_due: dict[str, Decimal]
    due_cny: Decimal | None  # None when a currency has no rate
    spend_by_currency: dict[str, Decimal]
    spend_cny: Decimal
    rate_note: str
    top_categories: list[tuple[str, Decimal]]
    category_rows: list[Row] = field(default_factory=list)

    @property
    def due_text(self) -> str:
        if all(v == 0 for v in self.amounts_due.values()):
            return "无需还款"
        if self.due_cny is not None:
            return f"¥ {money(self.due_cny)}"
        return "；".join(f"{c} {money(v)}" for c, v in self.amounts_due.items())

    @property
    def due_detail(self) -> str:
        foreign = {c: v for c, v in self.amounts_due.items() if c != "CNY" and v}
        return "；".join(f"{c} {money(v)}" for c, v in foreign.items())

    @property
    def spend_detail(self) -> str:
        return "；".join(f"{c} {money(v)}" for c, v in self.spend_by_currency.items())


@dataclass
class MonthSection:
    month: str
    summary: MonthlySummary
    missing: list[str]
    due_rows: list[Row]
    rate_notes: list[str]
    previous_total: Decimal | None = None

    @property
    def title(self) -> str:
        year, mon = self.month.split("-")
        return f"{year} 年 {int(mon)} 月"

    @property
    def delta_text(self) -> str:
        if self.missing:  # comparing a half-arrived month with a full one would mislead
            return "本月账单还没到齐，先不和上个月比较"
        if not self.previous_total:
            return "上个月没有数据，暂时没法比较"
        change = (self.summary.total_cny - self.previous_total) / self.previous_total
        arrow = "↑ 多了" if change > 0 else "↓ 少了"
        return f"比上个月{arrow} {abs(change):.0%}（上月 ¥ {money(self.previous_total)}）"

    @property
    def category_rows(self) -> list[Row]:
        """Real categories largest first, the tail folded into 其他; 未分类 last, in grey,
        because it is a to-do for rules.yaml rather than a kind of spending."""
        categories = dict(self.summary.categories)
        total = sum(categories.values(), ZERO)
        uncategorised = categories.pop(UNCATEGORISED, None)
        items = sorted(categories.items(), key=lambda kv: -kv[1])
        if len(items) > TOP_CATEGORIES:
            rest = sum((v for _, v in items[TOP_CATEGORIES - 1 :]), ZERO)
            items = items[: TOP_CATEGORIES - 1] + [("其他", rest)]
        if uncategorised is not None:
            items.append((UNCATEGORISED, uncategorised))
        rows = bar_rows(items, total)
        if uncategorised is not None:
            rows[-1].color = COLORS["context"]
        return rows

    @property
    def credit_rows(self) -> list[Row]:
        return [Row(f"{name}（已扣减）", money(v)) for name, v in self.summary.credits.items()]

    @property
    def card_rows(self) -> list[Row]:
        items = [(card_label(k), v) for k, v in self.summary.by_account.items()]
        return bar_rows(sorted(items, key=lambda kv: -kv[1]))

    @property
    def trend_rows(self) -> list[Row]:
        values = [v for _, v in self.summary.trend if v is not None and v > 0]
        top = max(values, default=ZERO)
        rows = []
        for month, value in reversed(self.summary.trend):  # newest first, like a list
            year, mon = month.split("-")
            label = f"{year} 年 {int(mon)} 月" + ("（本月）" if month == self.month else "")
            if value is None:
                rows.append(Row(label, "没有数据"))
                continue
            width = max(int(value / top * 100), MIN_BAR) if top and value > 0 else 0
            color = COLORS["accent"] if month == self.month else ""  # emphasis: this month
            rows.append(Row(label, f"¥ {money(value)}", width=width, color=color))
        return rows

    @property
    def merchant_rows(self) -> list[Row]:
        return bar_rows(self.summary.top_merchants)

    @property
    def uncategorised_rows(self) -> list[Row]:
        top = self.summary.uncategorised[:TOP_UNCATEGORISED_SHOWN]
        return [Row(name, money(v), f"{n} 笔") for name, n, v in top]


def bill_digest(bill: Bill, fx: FxRates, rules: Rules) -> BillDigest:
    """Part 1: the bill itself. Spending here is the whole bill, whatever the month."""
    rates, notes = {}, []
    for currency in sorted(
        {b.currency for b in bill.balances} | {t.currency for t in bill.transactions}
    ):
        try:
            rate = fx.rate(currency, bill.email_date)
        except RateUnavailable:
            rate = None
        rates[currency] = rate
        if rate is not None and rate.source != "identity":
            day = rate.rate_date.isoformat() if rate.rate_date else "配置汇率"
            notes.append(f"{currency} 按 {rate.rate_to_cny}（{day}）折算")

    amounts_due = {b.currency: b.amount_due for b in bill.balances}
    due_cny: Decimal | None = ZERO
    for currency, amount in amounts_due.items():
        if rates.get(currency) is None:
            due_cny = None
            break
        due_cny += amount * rates[currency].rate_to_cny

    spend_by_currency: dict[str, Decimal] = {}
    categories: dict[str, Decimal] = {}
    spend_cny = ZERO
    for t in bill.transactions:
        if t.txn_type not in SPENDING_TYPES + REDUCING_TYPES:
            continue
        spend_by_currency[t.currency] = spend_by_currency.get(t.currency, ZERO) + t.amount
        rate = rates.get(t.currency)
        if rate is None:
            continue
        cny = t.amount * rate.rate_to_cny
        spend_cny += cny
        if t.txn_type not in CREDIT_NAMES:
            name = rules.categorize(t.description_raw, t.txn_type)
            categories[name] = categories.get(name, ZERO) + cny

    top = [(k, cents(v)) for k, v in sorted(categories.items(), key=lambda kv: -kv[1])[:3]]
    digest = BillDigest(
        bank_name=BANK_NAMES.get(bill.bank, bill.bank),
        cards_text=" / ".join(bill.cards) or "未知卡号",
        statement_date=bill.statement_date,
        due_date=bill.due_date,
        status_note=STATUS_NOTES.get(bill.status, ""),
        amounts_due=amounts_due,
        due_cny=cents(due_cny) if due_cny is not None else None,
        spend_by_currency=spend_by_currency,
        spend_cny=cents(spend_cny),
        rate_note="汇率：" + "；".join(notes) if notes else "",
        top_categories=top,
    )
    digest.category_rows = bar_rows(top, sum(categories.values(), ZERO) or None)
    return digest


def months_for(bill: Bill) -> list[str]:
    """Calendar months the bill touches: its period, plus its transactions' months."""
    months = {t.trans_date.strftime("%Y-%m") for t in bill.transactions}
    if bill.period_start and bill.period_end:
        day = bill.period_start.replace(day=1)
        while day <= bill.period_end:
            months.add(day.strftime("%Y-%m"))
            day = (day + timedelta(days=32)).replace(day=1)
    if not months:
        months.add(bill.statement_date.strftime("%Y-%m"))
    return sorted(months)


def _month_last_day(month: str) -> date:
    return month_bounds(month)[1] - timedelta(days=1)


def expected_accounts(conn: sqlite3.Connection, month: str) -> list[str]:
    """Cards that had a bill within about two months of this month."""
    start, end = month_bounds(month)
    rows = conn.execute(
        "SELECT DISTINCT account_id FROM bills WHERE statement_date >= ? AND statement_date < ?"
        " ORDER BY account_id",
        ((start - EXPECTED_WINDOW).isoformat(), (end + EXPECTED_WINDOW).isoformat()),
    )
    return [r[0] for r in rows]


def missing_accounts(conn: sqlite3.Connection, month: str) -> list[str]:
    """Expected cards without the statement that covers the month's last day.

    That statement closes on or after the last day and within NEXT_STATEMENT of it; a much
    later statement does not count, or a missing month in between would go unnoticed.
    """
    last_day = _month_last_day(month)
    missing = []
    for account in expected_accounts(conn, month):
        covered = conn.execute(
            "SELECT 1 FROM bills WHERE account_id = ?"
            " AND COALESCE(period_end, statement_date) >= ? AND statement_date <= ?",
            (account, last_day.isoformat(), (last_day + NEXT_STATEMENT).isoformat()),
        ).fetchone()
        if not covered:
            missing.append(card_label(account))
    return missing


def due_rows(conn: sqlite3.Connection, month: str) -> list[Row]:
    """Each expected card's latest bill up to a month after this month: due date and amount.
    Shown as a list only; there are no reminders."""
    horizon = (_month_last_day(month) + timedelta(days=31)).isoformat()
    rows = []
    for account in expected_accounts(conn, month):
        row = conn.execute(
            "SELECT id FROM bills WHERE account_id = ? AND statement_date <= ?"
            " ORDER BY statement_date DESC LIMIT 1",
            (account, horizon),
        ).fetchone()
        if row is None:
            continue
        bill = load_bill(conn, row[0])
        owed = [f"{b.currency} {money(b.amount_due)}" for b in bill.balances if b.amount_due > 0]
        detail = f"{bill.statement_date:%m-%d} 账单"
        if not owed:
            rows.append(Row(card_label(account), "无需还款", detail))
        else:
            when = f"{bill.due_date:%m-%d} 前还款" if bill.due_date else "还款日未知"
            rows.append(Row(card_label(account), "；".join(owed), f"{detail} · {when}"))
    return rows


def month_section(conn: sqlite3.Connection, month: str, fx: FxRates, rules: Rules) -> MonthSection:
    summary = monthly_summary(conn, month, fx, rules)
    notes = []
    for line in summary.lines:
        if line.rate is not None and line.rate.source != "identity":
            day = line.rate.rate_date.isoformat() if line.rate.rate_date else "配置汇率"
            label = card_label(line.account_id)
            notes.append(f"{label} {line.currency} {line.rate.rate_to_cny}（{day}）")
    previous = summary.trend[-2][1] if len(summary.trend) >= 2 else None
    return MonthSection(
        month, summary, missing_accounts(conn, month), due_rows(conn, month), notes, previous
    )


_env = Environment(
    loader=PackageLoader("autobill.report", "templates"),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _subject(bill: Bill) -> str:
    cards = "/".join(bill.cards) or "未知卡号"
    return f"信用卡账单汇总｜{BANK_NAMES.get(bill.bank, bill.bank)} {cards}｜{bill.statement_date}"


def _preview_line(digest: BillDigest) -> str:
    """The grey line mail apps show under the subject."""
    return f"本期应还 {digest.due_text}，本期支出 ¥ {money(digest.spend_cny)}"


def render_html(subject: str, digest: BillDigest, months: list[MonthSection]) -> str:
    source = _env.get_template("bill_report.mjml.j2").render(
        subject=subject,
        preview=_preview_line(digest),
        digest=digest,
        months=months,
        money=money,
        c=COLORS,
    )
    result = mjml_to_html(source)
    if result.errors:
        raise ValueError(f"report template produced invalid MJML: {result.errors}")
    return result.html


def _plain_text(digest: BillDigest, months: list[MonthSection]) -> str:
    out = [
        f"{digest.bank_name} {digest.cards_text} {digest.statement_date} 账单",
        f"到期还款日：{digest.due_date or '无（本期无需还款）'}",
    ]
    if digest.status_note:
        out.append(digest.status_note)
    out.append("本期应还：" + "；".join(f"{c} {money(v)}" for c, v in digest.amounts_due.items()))
    out.append(f"本期支出：¥ {money(digest.spend_cny)}")
    for section in months:
        out += ["", "=" * 20]
        if section.missing:
            out.append("本月数据未完整：还缺 " + "、".join(section.missing))
        out.append(render_text(section.summary))
    return "\n".join(out)


def prepare(conn: sqlite3.Connection, bill_id: int, fx: FxRates, rules: Rules | None = None):
    rules = rules or load_rules()
    bill = load_bill(conn, bill_id)
    digest = bill_digest(bill, fx, rules)
    months = [month_section(conn, m, fx, rules) for m in months_for(bill)]
    return bill, digest, months


def build_email(
    conn: sqlite3.Connection,
    bill_id: int,
    fx: FxRates,
    sender: str,
    to_addr: str,
    rules: Rules | None = None,
) -> EmailMessage:
    """The report e-mail for one bill: a plain-text part and the HTML part."""
    bill, digest, months = prepare(conn, bill_id, fx, rules)
    subject = _subject(bill)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="autobill.invalid")
    msg["X-AutoBill-Report"] = "true"  # second guard against ever parsing our own reports
    msg.set_content(_plain_text(digest, months))
    msg.add_alternative(render_html(subject, digest, months), subtype="html")
    return msg


def preview_html(
    conn: sqlite3.Connection, bill_id: int, fx: FxRates, rules: Rules | None = None
) -> str:
    """Exactly the HTML of the e-mail, for a local preview file."""
    bill, digest, months = prepare(conn, bill_id, fx, rules)
    return render_html(_subject(bill), digest, months)
