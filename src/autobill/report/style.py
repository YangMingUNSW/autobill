"""Presentation helpers shared by the progress e-mail and the standard statement:
bank names, colours, money formatting and ranked bar rows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from autobill.categorize import UNCATEGORISED
from autobill.model import ZERO

BANK_NAMES = {"ABC": "农业银行", "CCB": "建设银行", "BOC": "中国银行"}
MIN_BAR = 2  # percent: even the smallest positive value gets a visible bar
TOP_CATEGORIES = 6  # more categories than this are folded into "其他"

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


# One emoji per category and per non-purchase type, shown in a tinted tile the way Copilot
# Money and Apple Card mark categories. Only emoji that need no variation selector.
CATEGORY_EMOJI = {
    "超市": "🛒",
    "交通": "🚇",
    "餐饮": "🍜",
    "旅行": "🧳",
    "住宿": "🏨",
    "购物": "👜",
    "医药": "💊",
    "订阅": "🔁",
    "通讯网络": "📶",
    "水电燃气": "💡",
    "健身": "💪",
    "烟酒": "🍷",
    "娱乐": "🎬",
    "网购": "📦",
    "微信/支付宝（未细分）": "💬",
    "利息": "🏦",
    "手续费": "🏦",
    "取现": "💵",
    "其他": "📂",
    UNCATEGORISED: "❔",
}
TYPE_EMOJI = {
    "还款": "💳",
    "退款": "🔄",
    "返现": "🎁",
    "购汇": "💱",
    "分期本金": "📅",
    "调整": "🧮",
}
DEFAULT_EMOJI = "🔖"  # a category the author added to rules.yaml


def emoji_for(category: str, tag: str = "") -> str:
    return TYPE_EMOJI.get(tag) or CATEGORY_EMOJI.get(category, DEFAULT_EMOJI)


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


# What a purchase actually cost, in the currency it was paid in. The yen and the yuan
# share "¥", so the yen keeps the country prefix (as CLDR writes it) and the two can never
# be mistaken for each other; a currency not listed here keeps its ISO code.
CURRENCY_SYMBOLS = {
    "CNY": "¥", "JPY": "JP¥", "USD": "US$", "AUD": "A$", "NZD": "NZ$", "CAD": "CA$",
    "EUR": "€", "GBP": "£", "CHF": "CHF ", "HKD": "HK$", "MOP": "MOP$", "TWD": "NT$",
    "SGD": "S$", "MYR": "RM", "KRW": "₩", "THB": "฿", "VND": "₫", "PHP": "₱",
    "IDR": "Rp", "INR": "₹", "RUB": "₽", "TRY": "₺", "AED": "AED ", "SEK": "SEK ",
    "NOK": "NOK ", "DKK": "DKK ", "PLN": "PLN ", "CZK": "CZK ", "HUF": "HUF ",
}  # fmt: skip
NO_DECIMALS = {"JPY", "KRW", "VND", "IDR", "HUF"}  # not divided into cents


def amount_with_symbol(value: Decimal, currency: str) -> str:
    """ "JP¥12,345", "US$168.50", "¥1,217.97"; a minus sign stays outside the symbol."""
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    digits = 0 if currency in NO_DECIMALS else 2
    body = f"{value:,.{digits}f}"
    return f"-{symbol}{body[1:]}" if body.startswith("-") else f"{symbol}{body}"


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


def category_bar_rows(categories: dict[str, Decimal]) -> list[Row]:
    """Real categories largest first, the tail folded into 其他; 未分类 last, in grey,
    because it is a to-do for rules.yaml rather than a kind of spending."""
    categories = dict(categories)
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
