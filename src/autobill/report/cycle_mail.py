"""The month's e-mail (docs/notify.md#账单月邮件): the report of report/cycle.py as HTML
and plain text, the message itself, and the conversation it belongs to.

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
import sqlite3
from collections.abc import Iterable
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from autobill.categorize import Rules, load_rules
from autobill.config import PortfolioCard
from autobill.fx import FxRates
from autobill.report.cycle import CycleReport, build_cycle_report
from autobill.report.render import env
from autobill.store.db import now as db_now


def render_cycle_html(report: CycleReport) -> str:
    return env.get_template("cycle_report.html.j2").render(r=report)


def plain_text(report: CycleReport) -> str:
    """For mail apps that show no HTML."""
    out = [report.title]
    if report.period:
        out.append(f"消费 {report.period}")
    out.append(report.status_line)
    if report.due_total is not None:
        out.append(f"本期应还：¥{report.due_total}")
    spent = f"本期消费：¥{report.spend_total}"
    out.append(f"{spent}（{report.spend_change}）" if report.spend_change else spent)
    if report.trend_shown:
        months = " · ".join(
            f"{b.label} " + (f"¥{b.value:,.0f}" if b.value is not None else "无账单")
            for b in report.trend
        )
        out.append(f"近 {len(report.trend)} 期：{months}")
    out.append("")
    for card in report.cards:
        line = f"{card.label}：{card.chip} {card.amount}".rstrip()
        if card.amount_orig:
            line += f"（{card.amount_orig}）"
        out.append(f"{line}（{card.note}）")
    if report.segments:
        out += ["", "分类："]
        out += [
            f"{s.label} {s.share} ¥{s.amount}" + (f"（{s.note}）" if s.note else "")
            for s in report.segments
        ]
    out += [
        "",
        f"全部 {report.transaction_count} 笔流水见 HTML 版本。",
        f"生成于 {report.generated_at} · 版本 {report.build}",
    ]
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
