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
    "住宿": "🏨",
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
