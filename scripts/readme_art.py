"""The README's artwork: the logo, an animated banner, an animated pipeline and the social
preview picture GitHub shows when the repository's link is shared.

Everything is drawn here as SVG: no image service, nothing fetched. The banner and the
pipeline come in a light and a dark variant for GitHub's two themes (the README picks one
with <picture>), and in English and Chinese for the two READMEs. The colours are the
e-mail's own (src/autobill/report/templates/_email.css), the numbers the demo month's
(scripts/demo_screenshots.py). GitHub shows an SVG as an image, so no script runs: the
motion is CSS inside the file. It plays once when the page loads (the pipeline's dots keep
flowing) and is left out for anyone whose system asks for reduced motion.

    uv run --with playwright python scripts/readme_art.py

Only the social preview PNG needs a browser: Playwright's Chromium, or an installed
Chrome with --browser <path>. Upload it by hand under Settings > General > Social preview.
"""

from __future__ import annotations

import argparse
import math
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"

SANS = (
    "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', 'Helvetica Neue', "
    "Arial, 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif"
)
ROUNDED = "ui-rounded, 'SF Pro Rounded', " + SANS
SPRING = "cubic-bezier(.32,.72,0,1)"  # the iOS sheet curve: quick, then settles
BOUNCE = "cubic-bezier(.3,1.35,.5,1)"  # a small overshoot, for bars that grow

THEMES = {
    "light": {
        "panel": "#f6f8fa",
        "card": "#ffffff",
        "line": "#d0d7de",
        "label": "#1f2328",
        "muted": "#59636e",
        "faint": "#818b98",
        "series": ("#2a78d6", "#eb6834", "#1baf7a", "#eda100"),
        "other": "#8e8e93",
        "shadow": 0.10,
    },
    "dark": {
        "panel": "#161b22",
        "card": "#21262d",
        "line": "#3d444d",
        "label": "#e6edf3",
        "muted": "#9198a1",
        "faint": "#6e7681",
        "series": ("#3987e5", "#d95926", "#199e70", "#c98500"),
        "other": "#8e8e93",
        "shadow": 0.45,
    },
}

TEXT = {
    "en": {
        "title": "AutoBill: credit-card statements, summed up in one e-mail",
        "tagline": ("Credit-card statements, summed up", "in one beautiful e-mail."),
        "pills": ("Rule-based", "Self-hosted", "Private"),
        "month": "September 2026 statement",
        "spent": "spent this period",
        "categories": ("Groceries", "Travel", "Dining", "Transport"),
        "trend": "Last 6 statements",
        "steps": (
            ("Banks", "e-statements"),
            ("Your mailbox", "read-only IMAP"),
            ("Parse & reconcile", "fixed rules, no AI"),
            ("SQLite", "on your server"),
            ("Summarise", "CNY · categories"),
            ("Apple Mail", "monthly · yearly"),
        ),
        "pipeline": "How AutoBill works: bank e-statements arrive in your mailbox, are read "
        "without changing anything, parsed and reconciled with fixed rules, stored in SQLite "
        "on your server, summarised in CNY by category, and sent to Apple Mail.",
    },
    "zh": {
        "title": "AutoBill：把信用卡账单汇总成一封好看的邮件",
        "tagline": ("信用卡账单，", "汇总成一封好看的邮件。"),
        "pills": ("纯规则解析", "自部署", "数据留在自己手里"),
        "month": "2026年9月账单",
        "spent": "本期消费",
        "categories": ("超市", "旅行", "餐饮", "交通"),
        "trend": "近 6 期",
        "steps": (
            ("银行", "电子账单"),
            ("专用邮箱", "只读 IMAP"),
            ("解析与对账", "固定规则，不用 AI"),
            ("SQLite", "在你的服务器上"),
            ("汇总", "折人民币 · 分类"),
            ("苹果邮件", "月报 · 年度回顾"),
        ),
        "pipeline": "AutoBill 的工作流程：银行的电子账单进到你的邮箱，只读取、不改动；用固定规则"
        "解析并对账，存进你服务器上的 SQLite，折成人民币、按分类汇总，发到苹果邮件。",
    },
}

TOTAL = "¥7,267"  # the demo month's spending (scripts/demo_screenshots.py)
SHARES = (0.30, 0.28, 0.19, 0.11)  # its four biggest categories; the rest is grey
TREND = (4609, 4949, 3808, 5125, 3848, 7267)  # its last six months
LOGO_SHARES = (0.34, 0.26, 0.22, 0.18)

# Line icons on a 24-unit grid, drawn with a round 1.8-unit stroke (the pipeline's nodes).
ICONS = {
    "bank": "M3 9.5 12 4l9 5.5M5.5 10.5v7.5M10 10.5v7.5M14 10.5v7.5M18.5 10.5v7.5M3 20.5h18",
    "mail": "M4 6h16a1 1 0 0 1 1 1v10.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z"
    "M3.6 6.8 12 13l8.4-6.2",
    "check": "M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM14 3v5h5"
    "M9 14.2l2.3 2.3 4.2-4.6",
    "db": "M5 6c0-1.66 3.13-3 7-3s7 1.34 7 3-3.13 3-7 3-7-1.34-7-3zM5 6v12c0 1.66 3.13 3 7 3"
    "s7-1.34 7-3V6M5 12c0 1.66 3.13 3 7 3s7-1.34 7-3",
    "chart": "M11 4.1A8 8 0 1 0 19.9 13H11zM14 2.6a7.5 7.5 0 0 1 7.4 7.4H14z",
    "phone": "M8.5 2.5h7a2 2 0 0 1 2 2v15a2 2 0 0 1-2 2h-7a2 2 0 0 1-2-2v-15a2 2 0 0 1 2-2z"
    "M10.6 5h2.8",
}
STEP_ICONS = ("bank", "mail", "check", "db", "chart", "phone")


def text_width(text: str, size: float) -> float:
    """A rough width, for sizing pills: CJK characters are square, Latin ones narrower."""
    return sum(size if ord(c) > 0x2E80 else size * 0.56 for c in text)


def donut(cx: float, cy: float, r: float, width: float, parts, gap: float) -> str:
    """Arcs from twelve o'clock, clockwise: one circle per (share, colour), with gaps."""
    c = 2 * math.pi * r
    out, start = [], 0.0
    for share, color in parts:
        length = max(share * c - gap, 0.5)
        out.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-dasharray="{length:.2f} {c:.2f}" '
            f'stroke-dashoffset="{-start * c:.2f}" transform="rotate(-90 {cx} {cy})"/>'
        )
        start += share
    return "".join(out)


def logo_group(x: float, y: float, size: float, uid: str) -> str:
    """The app icon: the e-mail's category donut on a white rounded square."""
    series = THEMES["light"]["series"]
    return (
        f'<g transform="translate({x} {y}) scale({size / 128:.4f})">'
        f'<defs><linearGradient id="{uid}" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#eef1f6"/>'
        "</linearGradient></defs>"
        f'<rect x="4" y="4" width="120" height="120" rx="28" fill="url(#{uid})" '
        'stroke="#1f2328" stroke-opacity=".12" stroke-width="1.5"/>'
        + donut(64, 64, 33, 17, zip(LOGO_SHARES, series, strict=True), 4)
        + "</g>"
    )


def logo_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" '
        'viewBox="0 0 128 128" role="img" aria-label="AutoBill"><title>AutoBill</title>'
        + logo_group(0, 0, 128, "logo-bg")
        + "</svg>\n"
    )


def banner_style(t: dict, animate: bool) -> str:
    css = f"""
text {{ font-family: {SANS}; }}
.name {{ font-weight: 700; font-size: 64px; fill: {t["label"]}; letter-spacing: -1px; }}
.tag {{ font-size: 26px; fill: {t["muted"]}; }}
.pill {{ font-weight: 500; font-size: 15px; fill: {t["label"]}; }}
.eyebrow {{ font-size: 16px; fill: {t["muted"]}; }}
.total {{ font-family: {ROUNDED}; font-weight: 700; font-size: 46px; fill: {t["label"]}; }}
.cat {{ font-size: 16px; fill: {t["label"]}; }}
.pct {{ font-size: 16px; fill: {t["muted"]}; font-variant-numeric: tabular-nums; }}
.small {{ font-size: 13px; fill: {t["faint"]}; }}
"""
    if not animate:
        return css
    return (
        css
        + f"""
.up {{ animation: up .8s {SPRING} both; }}
.grow {{ transform-box: fill-box; transform-origin: 50% 100%; animation: grow .7s {BOUNCE} both; }}
@keyframes up {{ from {{ opacity: 0; transform: translateY(16px); }}
                to {{ opacity: 1; transform: none; }} }}
@keyframes grow {{ from {{ transform: scaleY(0); }} to {{ transform: none; }} }}
@media (prefers-reduced-motion: reduce) {{ .up, .grow, .sweep {{ animation: none; }} }}
"""
    )


def banner_body(t: dict, x: dict, animate: bool) -> str:
    """The banner's left block (icon, name, tagline, pills) and its card (the month: total,
    categories, donut, six months), on a 1200 x 400 canvas without its background."""
    parts = [f'<g class="up">{logo_group(72, 92, 84, "icon-bg")}']
    parts.append('<text class="name" x="176" y="157">AutoBill</text>')
    for i, line in enumerate(x["tagline"]):
        parts.append(f'<text class="tag" x="74" y="{230 + i * 36}">{escape(line)}</text>')
    px = 74.0
    for pill in x["pills"]:
        w = text_width(pill, 15) + 32
        parts.append(
            f'<rect x="{px:.1f}" y="300" width="{w:.1f}" height="36" rx="18" '
            f'fill="{t["card"]}" stroke="{t["line"]}"/>'
            f'<text class="pill" x="{px + w / 2:.1f}" y="323" text-anchor="middle">'
            f"{escape(pill)}</text>"
        )
        px += w + 10
    parts.append("</g>")

    # The card, as the e-mail draws the month.
    cx0, cy0, cw, ch = 688, 50, 452, 300
    delay = ' style="animation-delay:.12s"' if animate else ""
    parts.append(f'<g class="up"{delay}>')
    parts.append(
        f'<rect x="{cx0}" y="{cy0}" width="{cw}" height="{ch}" rx="24" fill="{t["card"]}" '
        f'stroke="{t["line"]}" filter="url(#shadow)"/>'
    )
    parts.append(f'<text class="eyebrow" x="{cx0 + 28}" y="{cy0 + 44}">{x["month"]}</text>')
    parts.append(f'<text class="total" x="{cx0 + 26}" y="{cy0 + 98}">{TOTAL}</text>')
    parts.append(f'<text class="eyebrow" x="{cx0 + 28}" y="{cy0 + 126}">{x["spent"]}</text>')
    for i, (name, share) in enumerate(zip(x["categories"], SHARES, strict=True)):
        y = cy0 + 176 + i * 30
        style = f' style="animation-delay:{0.55 + i * 0.08:.2f}s"' if animate else ""
        parts.append(
            f'<g class="up"{style}><circle cx="{cx0 + 34}" cy="{y - 5.5}" r="5.5" '
            f'fill="{t["series"][i]}"/><text class="cat" x="{cx0 + 49}" y="{y}">{name}</text>'
            f'<text class="pct" x="{cx0 + 252}" y="{y}" text-anchor="end">'
            f"{round(share * 100)}%</text></g>"
        )
    dx, dy, r = cx0 + 352, cy0 + 108, 62
    ring = list(zip(SHARES, t["series"], strict=True)) + [(1 - sum(SHARES), t["other"])]
    mask = ' mask="url(#reveal)"' if animate else ""
    parts.append(f"<g{mask}>{donut(dx, dy, r, 22, ring, 3)}</g>")
    base, top = cy0 + 268, max(TREND)
    for i, value in enumerate(TREND):
        h = 62 * value / top
        color = t["series"][0] if i == len(TREND) - 1 else t["other"]
        opacity = "" if i == len(TREND) - 1 else ' fill-opacity=".55"'
        style = f' style="animation-delay:{0.7 + i * 0.06:.2f}s"' if animate else ""
        parts.append(
            f'<rect class="grow"{style} x="{dx - 67 + i * 24}" y="{base - h:.1f}" width="14" '
            f'height="{h:.1f}" rx="3" fill="{color}"{opacity}/>'
        )
    parts.append(f'<text class="small" x="{dx - 67}" y="{base + 20}">{x["trend"]}</text>')
    parts.append("</g>")
    return "".join(parts)


def banner_defs(t: dict, animate: bool) -> str:
    dx, dy, r = 688 + 352, 50 + 108, 62
    c = 2 * math.pi * r
    defs = (
        '<filter id="shadow" x="-20%" y="-20%" width="140%" height="150%">'
        f'<feDropShadow dx="0" dy="10" stdDeviation="16" flood-color="#000" '
        f'flood-opacity="{t["shadow"]}"/></filter>'
    )
    if animate:
        # A white ring in the mask sweeps round once and uncovers the donut behind it.
        defs += (
            f"<style>.sweep {{ animation: sweep 1.1s cubic-bezier(.45,0,.2,1) .45s both; }}"
            f"@keyframes sweep {{ from {{ stroke-dashoffset: {c:.2f}; }} "
            "to { stroke-dashoffset: 0; } }</style>"
            f'<mask id="reveal"><circle class="sweep" cx="{dx}" cy="{dy}" r="{r}" fill="none" '
            f'stroke="#fff" stroke-width="40" stroke-dasharray="{c:.2f} {c:.2f}" '
            f'transform="rotate(-90 {dx} {dy})"/></mask>'
        )
    return f"<defs>{defs}</defs>"


def banner_svg(theme: str, lang: str) -> str:
    t, x = THEMES[theme], TEXT[lang]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="400" '
        f'viewBox="0 0 1200 400" role="img" aria-label="{escape(x["title"])}">'
        f"<title>{escape(x['title'])}</title><style>{banner_style(t, True)}</style>"
        + banner_defs(t, True)
        + f'<rect width="1200" height="400" rx="28" fill="{t["panel"]}"/>'
        + banner_body(t, x, True)
        + "</svg>\n"
    )


def pipeline_svg(theme: str, lang: str) -> str:
    """Six steps from the bank to the phone, with dots flowing between them."""
    t, x = THEMES[theme], TEXT[lang]
    colors = (t["other"], t["series"][0], t["series"][2], t["series"][3], t["series"][1],
              t["series"][0])  # fmt: skip
    style = f"""
text {{ font-family: {SANS}; text-anchor: middle; }}
.step {{ font-weight: 600; font-size: 17px; fill: {t["label"]}; }}
.sub {{ font-size: 14px; fill: {t["muted"]}; }}
.node {{ animation: up .7s {SPRING} both; }}
.flow {{ stroke-dasharray: 1.5 9.5; animation: flow .9s linear infinite; }}
@keyframes up {{ from {{ opacity: 0; transform: translateY(12px); }}
                to {{ opacity: 1; transform: none; }} }}
@keyframes flow {{ to {{ stroke-dashoffset: -11; }} }}
@media (prefers-reduced-motion: reduce) {{ .node, .flow {{ animation: none; }} }}
"""
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="230" '
        f'viewBox="0 0 1200 230" role="img" aria-label="{escape(x["pipeline"])}">'
        f"<title>{escape(x['pipeline'])}</title><style>{style}</style>"
    ]
    centers = [110 + i * 196 for i in range(6)]
    for i in range(5):
        a, b = centers[i] + 54, centers[i + 1] - 54
        parts.append(
            f'<line class="flow" x1="{a}" y1="84" x2="{b - 6}" y2="84" stroke="{t["faint"]}" '
            'stroke-width="3.5" stroke-linecap="round"/>'
            f'<path d="M{b - 9} 77l7 7-7 7" fill="none" stroke="{t["faint"]}" '
            'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    steps = zip(x["steps"], STEP_ICONS, colors, strict=True)
    for i, ((step, sub), icon, color) in enumerate(steps):
        cx = centers[i]
        parts.append(
            f'<g class="node" style="animation-delay:{i * 0.09:.2f}s">'
            f'<circle cx="{cx}" cy="84" r="42" fill="{color}" fill-opacity=".14" '
            f'stroke="{color}" stroke-opacity=".35"/>'
            f'<path d="{ICONS[icon]}" transform="translate({cx - 21} 63) scale(1.75)" '
            f'fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" '
            'stroke-linejoin="round"/>'
            f'<text class="step" x="{cx}" y="162">{escape(step)}</text>'
            f'<text class="sub" x="{cx}" y="187">{escape(sub)}</text></g>'
        )
    parts.append("</svg>\n")
    return "".join(parts)


def social_svg() -> str:
    """1280 x 640, the size GitHub asks for: the banner's picture, bigger, with the address."""
    t, x = THEMES["light"], TEXT["en"]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640" '
        'viewBox="0 0 1280 640">'
        f"<style>{banner_style(t, False)}"
        f".url {{ font-family: {SANS}; font-size: 22px; fill: {t['faint']}; }}</style>"
        + banner_defs(t, False)
        + '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#f6f8fa"/><stop offset="1" stop-color="#e9eef6"/>'
        '</linearGradient></defs><rect width="1280" height="640" fill="url(#bg)"/>'
        + f'<g transform="translate(40 110)">{banner_body(t, x, False)}</g>'
        + '<text class="url" x="114" y="580">github.com/YangMingUNSW/autobill</text>'
        + "</svg>\n"
    )


def write(name: str, content: str) -> Path:
    path = OUT / name
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def social_png(browser: str | None) -> Path:
    from playwright.sync_api import sync_playwright

    path = OUT / "social-preview.png"
    with sync_playwright() as p:
        chromium = p.chromium.launch(executable_path=browser) if browser else p.chromium.launch()
        page = chromium.new_page(viewport={"width": 1280, "height": 640})
        page.set_content(f"<body style='margin:0'>{social_svg()}</body>")
        page.screenshot(path=path)
        chromium.close()
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--browser", help="a Chromium/Chrome executable, for the PNG")
    parser.add_argument("--no-png", action="store_true", help="skip the social preview PNG")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    written = [write("logo.svg", logo_svg())]
    for theme in THEMES:
        for lang in TEXT:
            suffix = "" if lang == "en" else f".{lang}"
            written.append(write(f"banner-{theme}{suffix}.svg", banner_svg(theme, lang)))
            written.append(write(f"pipeline-{theme}{suffix}.svg", pipeline_svg(theme, lang)))
    if not args.no_png:
        written.append(social_png(args.browser))
    for path in written:
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
