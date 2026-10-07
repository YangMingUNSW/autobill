"""Charts drawn as inline SVG for the e-mails (docs/notify.md#邮件内容): columns for recent
statement months and a ring of shares. Colours come from CSS classes, so a chart follows
light and dark mode; every number a chart shows is in its aria-label too.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from markupsafe import Markup, escape

from autobill.model import ZERO

if TYPE_CHECKING:
    from autobill.report.cycle import MonthBar, Segment


def trend_svg(bars: list[MonthBar], name: str = "") -> Markup:
    """One column per statement month, Apple Card's monthly activity: this month in the
    accent, the months before in grey (emphasis, not categories). Marks follow the dataviz
    spec: 4px rounded top, square base, one hairline baseline. Every month's value is
    written over its column (the difference should read in numbers too), this month's in the
    label colour and bold, the others in grey; they are also in the aria-label and the
    plain-text part. A month without statements is a dash, so the months stay evenly spaced.
    `name` is what the aria-label calls the chart (default "近 6 期消费")."""
    if not bars:
        return Markup("")
    # viewBox units: text is sized ~20 so it is ~11px when a phone scales 600 to ~330.
    width, height, top_pad, bottom_pad = 600, 210, 40, 34
    plot_h = height - top_pad - bottom_pad
    base = height - bottom_pad
    slot = width / len(bars)
    bar_w = min(40.0, slot * 0.56)  # six months: ~22px on a phone, under the 24px cap
    peak = max((b.value for b in bars if b.value is not None and b.value > 0), default=ZERO)
    said = "，".join(
        f"{b.label} " + (f"¥{b.value:,.0f}" if b.value is not None else "无账单") for b in bars
    )
    name = name or f"近 {len(bars)} 期消费"
    parts = [
        f'<svg class="trend" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(name + "（人民币）：" + said)}">',
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
        parts.append(
            f'<text class="value{now}" x="{cx:.1f}" y="{top - 8:.1f}" text-anchor="middle">'
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
    said = "，".join(f"{s.label} {s.share}" for s in segments)
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
