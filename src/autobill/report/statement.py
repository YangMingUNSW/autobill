"""The standard statement: every bank's bill in one well-designed template, every
transaction included. See docs/statement.md.

Layout follows the structure US card statements are required to use (payment information,
account summary, transactions, fees), with Apple Card-style spending overview and
Monzo-style transactions grouped by day. It is AutoBill's own document - no bank branding -
and says it is derived from the bank's e-statement, which remains authoritative.

Plain HTML with inline CSS and inline SVG: no JavaScript and no external resources, so it
opens anywhere (browser, iPhone Files) and prints to PDF unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup

from autobill.categorize import Rules, load_rules
from autobill.fx import FxRates, Rate, RateUnavailable
from autobill.model import ZERO, Bill, BillBalance, Transaction, TxnType
from autobill.report.monthly import SPENDING_TYPES, cents
from autobill.report.style import (
    BANK_NAMES,
    COLORS,
    Row,
    category_bar_rows,
    money,
)

TYPE_LABELS = {
    TxnType.PURCHASE: "消费",
    TxnType.REFUND: "退款",
    TxnType.REPAYMENT: "还款",
    TxnType.FEE: "手续费",
    TxnType.INTEREST: "利息",
    TxnType.CASH: "取现",
    TxnType.INSTALLMENT: "分期本金",
    TxnType.REBATE: "返现",
    TxnType.FX_TRANSFER: "购汇",
    TxnType.ADJUSTMENT: "调整",
}
# Rows listed for completeness but not counted as spending (docs/notify.md#统计口径).
NOT_SPENDING = {TxnType.INSTALLMENT, TxnType.REPAYMENT, TxnType.FX_TRANSFER, TxnType.ADJUSTMENT}
WEEKDAYS = "一二三四五六日"
STATUS_BADGES = {
    "OK": ("ok", "✓", "对账通过"),
    "WARN": ("warn", "⚠", "对账有警告"),
    "UNVERIFIED": ("warn", "?", "未对账"),
}


@dataclass
class TxnLine:
    line_no: int
    title: str
    meta: list[str]
    amount: str  # signed, settlement currency
    tag: str = ""  # 还款 / 返现 / 购汇 ... ; empty for plain purchases
    excluded: bool = False  # listed but not counted as spending
    category: str = ""


@dataclass
class DayGroup:
    label: str
    lines: list[TxnLine]


@dataclass
class BalanceLine:
    """One currency's summary block, as the account-summary equation."""

    currency: str
    previous: str
    charges: str
    credits: str
    adjustments: str
    new: str
    minimum: str
    due: str

    @classmethod
    def of(cls, b: BillBalance) -> BalanceLine:
        return cls(
            currency=b.currency,
            previous=money(b.previous_balance - b.previous_deposit),
            charges=money(b.new_charges + b.interest_fees),
            credits=money(-b.payments_credits),
            adjustments=money(b.adjustments),
            new=money(b.amount_due - b.deposit),
            minimum=money(b.min_payment) if b.min_payment is not None else "—",
            due=money(b.amount_due),
        )


@dataclass
class TypeTotal:
    label: str
    count: int
    amounts: list[str]  # one per currency, signed


@dataclass
class StatementView:
    title: str
    bank_name: str
    card: str
    period: str
    statement_date: date
    due_date: date | None
    status: tuple[str, str, str]  # (css class, icon, text)
    warnings: list[str]
    nothing_due: bool
    due_cny: str | None
    balances: list[BalanceLine]
    spend_cny: str
    spend_by_currency: str
    category_rows: list[Row]
    daily_chart: Markup
    daily_caption: str
    type_totals: list[TypeTotal]
    days: list[DayGroup]
    fx_notes: list[str]
    source: str
    generated_at: str
    transaction_count: int = field(default=0)
    currencies: str = ""
    # Unformatted values, for the progress e-mail that adds bills up
    due_cny_value: Decimal | None = None
    spend_cny_value: Decimal = ZERO
    categories: dict[str, Decimal] = field(default_factory=dict)
    chart_days: list[date] = field(default_factory=list)
    merchants: dict[str, Decimal] = field(default_factory=dict)  # CNY spent per merchant
    daily_values: dict[date, Decimal] = field(default_factory=dict)


def _day_label(day: date) -> str:
    return f"{day.month} 月 {day.day} 日 · 周{WEEKDAYS[day.weekday()]}"


def _line(t: Transaction, category: str, show_card: bool, show_currency: bool) -> TxnLine:
    title = t.merchant or t.description_raw
    meta: list[str] = []
    if t.txn_type == TxnType.PURCHASE:
        meta.append(category)
    if t.merchant and t.merchant_location:
        meta.append(t.merchant_location)
    if t.orig_amount is not None and t.orig_currency:
        meta.append(f"{t.orig_currency} {money(t.orig_amount)}")
    if t.fx_rate is not None:
        meta.append(f"汇率 {t.fx_rate.normalize()}")
    if t.installment and t.installment not in title:
        meta.append(f"第 {t.installment} 期")
    if t.card_last4 and show_card:
        meta.append(f"尾号 {t.card_last4}")
    if t.synthetic:
        meta.append("银行未列明细，由对账补出")
    if t.post_date and t.post_date != t.trans_date:
        meta.append(f"{t.post_date:%m-%d} 入账")
    excluded = t.txn_type in NOT_SPENDING
    tag = "" if t.txn_type == TxnType.PURCHASE else TYPE_LABELS[t.txn_type]
    return TxnLine(
        line_no=t.line_no,
        title=title,
        meta=meta,
        amount=f"{t.currency} {money(t.amount)}" if show_currency else money(t.amount),
        tag=tag,
        excluded=excluded,
        category=category,
    )


def _chart_days(bill: Bill) -> list[date]:
    """The statement period, or (BOC prints none) the span of its transaction dates."""
    if bill.period_start and bill.period_end:
        start, end = bill.period_start, bill.period_end
    elif bill.transactions:
        start = min(t.trans_date for t in bill.transactions)
        end = max(t.trans_date for t in bill.transactions)
    else:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def daily_svg(days: list[date], values: dict[date, Decimal], average: bool = False) -> Markup:
    """Column per day of spending in CNY. Marks follow the dataviz spec: <= 24px wide,
    4px rounded top and square base, one hairline baseline, only the largest day labelled,
    text in text colours (never the bar colour). Colours come from CSS variables so the
    chart follows light and dark mode. With `average`, a dashed line marks the daily
    average over the whole period, as Screen Time does; the caption names it."""
    if not days:
        return Markup("")
    # viewBox units: text is sized ~20 so it is ~11px when a phone scales 600 to ~340.
    width, height, top_pad, bottom_pad = 600, 190, 34, 34
    plot_h = height - top_pad - bottom_pad
    slot = width / len(days)
    bar_w = min(24.0, slot * 0.72)
    peak = max(values.values(), default=ZERO)
    parts = [
        f'<svg class="daily" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="每日消费（人民币）">',  # scales evenly: no squashed text
        f'<line class="axis" x1="0" y1="{height - bottom_pad}" x2="{width}" '
        f'y2="{height - bottom_pad}" />',
    ]
    peak_day = None
    for i, day in enumerate(days):
        value = values.get(day, ZERO)
        if value <= 0 or peak <= 0:
            continue
        h = max(float(value / peak) * plot_h, 2.0)
        x = i * slot + (slot - bar_w) / 2
        y = height - bottom_pad - h
        r = min(4.0, bar_w / 2, h)
        parts.append(
            f'<path class="bar" d="M{x:.1f},{height - bottom_pad} V{y + r:.1f} '
            f"Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + bar_w - r:.1f} "
            f'Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{height - bottom_pad} Z" />'
        )
        if value == peak and peak_day is None:
            peak_day = (x + bar_w / 2, y, value)
    if peak_day is not None:
        cx, cy, value = peak_day
        anchor = "start" if cx < 60 else "end" if cx > width - 60 else "middle"
        parts.append(
            f'<text class="value" x="{cx:.1f}" y="{cy - 8:.1f}" text-anchor="{anchor}">'
            f"¥{value:,.0f}</text>"
        )
    mean = sum(values.values(), ZERO) / len(days)
    if average and peak > 0 and mean > 0:
        y = height - bottom_pad - float(mean / peak) * plot_h
        parts.append(f'<line class="avg" x1="0" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" />')
    first, last = days[0], days[-1]
    parts.append(f'<text class="tick" x="0" y="{height - 6}">{first:%m-%d}</text>')
    parts.append(
        f'<text class="tick" x="{width}" y="{height - 6}" text-anchor="end">{last:%m-%d}</text>'
    )
    parts.append("</svg>")
    return Markup("".join(parts))


def build_view(bill: Bill, fx: FxRates, rules: Rules, now: datetime | None = None) -> StatementView:
    rates: dict[str, Rate | None] = {}
    fx_notes: list[str] = []
    currencies = sorted(
        {b.currency for b in bill.balances} | {t.currency for t in bill.transactions}
    )
    for currency in currencies:
        try:
            rates[currency] = fx.rate(currency, bill.email_date)
        except RateUnavailable:
            rates[currency] = None
            fx_notes.append(f"{currency} 取不到汇率，没有折算人民币")
        rate = rates[currency]
        if rate is not None and rate.source != "identity":
            day = rate.rate_date.isoformat() if rate.rate_date else "配置汇率"
            fx_notes.append(f"{currency} 按 {rate.rate_to_cny} 折算人民币（{day}，Frankfurter）")
    for t in bill.transactions:
        if t.fx_rate is not None:
            fx_notes.append(f"{t.trans_date:%m-%d} 自动购汇，银行汇率 {t.fx_rate.normalize()}")

    def cny(amount: Decimal, currency: str) -> Decimal | None:
        rate = rates.get(currency)
        return amount * rate.rate_to_cny if rate is not None else None

    # Spending: the same types and signs as the monthly report.
    categories: dict[str, Decimal] = {}
    spend_cny = ZERO
    spend_by_currency: dict[str, Decimal] = {}
    daily: dict[date, Decimal] = {}
    merchants: dict[str, Decimal] = {}
    counted = set(SPENDING_TYPES) | {TxnType.REFUND, TxnType.REBATE}
    for t in bill.transactions:
        if t.txn_type not in counted:
            continue
        spend_by_currency[t.currency] = spend_by_currency.get(t.currency, ZERO) + t.amount
        value = cny(t.amount, t.currency)
        if value is None:
            continue
        spend_cny += value
        if t.txn_type in SPENDING_TYPES:
            name = rules.categorize(t.description_raw, t.txn_type, t.merchant)
            categories[name] = categories.get(name, ZERO) + value
            daily[t.trans_date] = daily.get(t.trans_date, ZERO) + value
            merchant = t.merchant or t.description_raw
            merchants[merchant] = merchants.get(merchant, ZERO) + value

    totals: dict[TxnType, list] = {}
    for t in bill.transactions:
        entry = totals.setdefault(t.txn_type, [0, {}])
        entry[0] += 1
        entry[1][t.currency] = entry[1].get(t.currency, ZERO) + t.amount
    type_totals = [
        TypeTotal(
            TYPE_LABELS[kind],
            count,
            [f"{c} {money(v)}" for c, v in sorted(amounts.items())],
        )
        for kind, (count, amounts) in sorted(
            totals.items(), key=lambda kv: list(TxnType).index(kv[0])
        )
    ]

    show_card = len({t.card_last4 for t in bill.transactions if t.card_last4}) > 1
    show_currency = len({t.currency for t in bill.transactions}) > 1  # else the header says it
    groups: dict[date, list[TxnLine]] = {}
    for t in sorted(bill.transactions, key=lambda t: (t.trans_date, t.line_no)):
        category = rules.categorize(t.description_raw, t.txn_type, t.merchant)
        groups.setdefault(t.trans_date, []).append(_line(t, category, show_card, show_currency))
    days = [DayGroup(_day_label(d), lines) for d, lines in groups.items()]

    due_cny: Decimal | None = ZERO
    for b in bill.balances:
        value = cny(b.amount_due, b.currency)
        if value is None:
            due_cny = None
            break
        due_cny += value

    chart_days = _chart_days(bill)
    peak = max(daily.values(), default=ZERO)
    active = sum(1 for d in chart_days if daily.get(d, ZERO) > 0)
    caption = f"{len(chart_days)} 天里有 {active} 天有消费" + (
        f"，最多的一天 ¥{money(cents(peak))}" if peak else ""
    )
    bank_name = BANK_NAMES.get(bill.bank, bill.bank)
    card = " / ".join(bill.cards) or "未知卡号"
    period = (
        f"{bill.period_start} 至 {bill.period_end}"
        if bill.period_start and bill.period_end
        else f"账单日 {bill.statement_date}"
    )
    return StatementView(
        title=f"AutoBill 标准账单 · {bank_name} 尾号 {card} · {bill.statement_date}",
        bank_name=bank_name,
        card=card,
        period=period,
        statement_date=bill.statement_date,
        due_date=bill.due_date,
        status=STATUS_BADGES.get(bill.status, STATUS_BADGES["UNVERIFIED"]),
        warnings=list(bill.warnings),
        nothing_due=all(b.amount_due == 0 for b in bill.balances),
        due_cny=money(cents(due_cny)) if due_cny is not None else None,
        balances=[BalanceLine.of(b) for b in bill.balances],
        spend_cny=money(cents(spend_cny)),
        spend_by_currency="；".join(
            f"{c} {money(v)}" for c, v in sorted(spend_by_currency.items())
        ),
        category_rows=category_bar_rows(categories),
        daily_chart=daily_svg(chart_days, daily),
        daily_caption=caption,
        type_totals=type_totals,
        days=days,
        fx_notes=fx_notes,
        source=f"解析器 {bill.parser_name} v{bill.parser_version} · 银行邮件 {bill.email_date}",
        generated_at=(now or datetime.now()).strftime("%Y-%m-%d %H:%M"),
        transaction_count=len(bill.transactions),
        currencies="、".join(sorted({t.currency for t in bill.transactions})),
        due_cny_value=due_cny,
        spend_cny_value=spend_cny,
        categories=categories,
        chart_days=chart_days,
        merchants=merchants,
        daily_values=daily,
    )


_env = Environment(
    loader=PackageLoader("autobill.report", "templates"),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_statement_html(
    bill: Bill, fx: FxRates, rules: Rules | None = None, now: datetime | None = None
) -> str:
    view = build_view(bill, fx, rules or load_rules(), now)
    return _env.get_template("statement.html.j2").render(v=view, c=COLORS)
