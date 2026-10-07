"""The year in review's filter by month and category (docs/notify.md#年度回顾): every
choice written into the e-mail beforehand, two groups of radio buttons and one CSS rule
per value, so the chosen month and category show without a script.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from markupsafe import Markup

from autobill.categories.names import shown_name
from autobill.ledger import cents
from autobill.model import ZERO
from autobill.report import drill
from autobill.report.charts import donut_svg
from autobill.report.cycle import Segment, make_segment
from autobill.report.style import emoji_for, money, share

TOP_MERCHANTS = 10
TOP_VISITS = 5
SHOWN_CATEGORIES = 8  # the rest fold away behind one row, as the month's transactions do
TOP_SHOPS = 5  # the shops listed for a month or a category chosen
BAR_PX = 92  # the tallest bar; every bar's height is a share of it
STRIP_PX = 28  # the tallest column of a shop's twelve months


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
        Choice(j, make_segment(name, v, whole, tones.get(name, "other"), emoji_for(name)))
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
            parts.append(make_segment(name, v, whole, tone))
        else:
            rest += v
    if rest > 0:
        parts.append(make_segment("其余", rest, whole, "other"))
    caption = f"{_month_name(month)}消费"
    donut = donut_svg(parts, f"¥{spent:,.0f}", caption) if whole > 0 else Markup("")
    folded_share = share(folded_total, whole) if whole > 0 else ""
    return MonthList(month, shown, folded, money(cents(folded_total)), folded_share, donut)


def build_explorer(
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
