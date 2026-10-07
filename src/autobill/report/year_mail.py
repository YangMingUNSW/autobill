"""The year in review as an e-mail (docs/notify.md#年度回顾): the report of
report/year.py as HTML and plain text, and the message itself."""

from __future__ import annotations

import sqlite3
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from autobill.categorize import Rules, load_rules
from autobill.fx import FxRates
from autobill.report.render import env
from autobill.report.year import YearReport, build_year_report


def render_year_html(report: YearReport) -> str:
    """The e-mail, its lines stripped of the template's indentation: every choice of the
    filter is written in beforehand, so the saving adds up (about a tenth)."""
    html = env.get_template("year_review.html.j2").render(r=report)
    return chr(10).join(line.strip() for line in html.splitlines() if line.strip())


def year_plain_text(report: YearReport) -> str:
    """For mail apps that show no HTML, and for `autobill year-review` in a terminal."""
    total = f"全年消费：¥{report.spend_total}"
    months = " · ".join(
        f"{b.label} " + (f"¥{b.value:,.0f}" if b.value is not None else "无消费")
        for b in report.bars
    )
    out = [
        report.title,
        report.span,
        "",
        f"{total}（{report.change}）" if report.change else total,
        f"月均 {report.average} · {report.purchases} 笔消费",
        f"每个月：{months}",
    ]
    if report.categories:
        out += ["", "分类："]
        out += [f"{s.name} {s.share} ¥{s.amount}" for s in report.categories]
    if report.merchants:
        out += ["", "花得最多的商户："]
        out += [f"{i}. {m.name} ¥{m.amount}" for i, m in enumerate(report.merchants, 1)]
    if report.visits:
        out += ["", "去得最多的店："]
        out += [f"{v.name} {v.count} 次 ¥{v.amount}" for v in report.visits]
    if report.currencies:
        out += ["", "用哪些货币消费："]
        out += [
            f"{c.name} {c.share} ¥{c.amount}" + (f"（{c.note}）" if c.note else "")
            for c in report.currencies
        ]
    if report.cards:
        out += ["", "各张卡："]
        out += [f"{c.label} ¥{c.amount}（{c.share}）" for c in report.cards]
    out += [
        "",
        f"返现 +¥{report.rebates_text} · 利息和手续费 ¥{report.charges_text}",
        f"生成于 {report.generated_at} · 版本 {report.build}",
    ]
    return "\n".join(out)


def build_year_email(
    conn: sqlite3.Connection,
    year: int,
    fx: FxRates,
    sender: str,
    to_addr: str,
    *,
    rules: Rules | None = None,
) -> tuple[EmailMessage, YearReport]:
    """The year's review as an e-mail of its own: no attachment, no thread."""
    report = build_year_report(conn, year, fx, rules or load_rules(conn))
    msg = EmailMessage()
    msg["Subject"] = report.subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="autobill.invalid")
    msg["X-AutoBill-Report"] = "true"  # never parsed as a statement (fetch/mime.py)
    msg.set_content(year_plain_text(report))
    msg.add_alternative(render_year_html(report), subtype="html")
    return msg, report
