"""Alert e-mails: things the author has to act on. See docs/notify.md#提醒邮件.

Once AutoBill runs unattended on a server, nobody reads its output. So these are e-mailed:

  * an e-mail AutoBill does not recognise (a new bank, or a bank's own notice);
  * a statement that failed to parse (the bank probably changed its template);
  * a card number never seen before (a new card - or a replaced one needing an alias);
  * the mailbox login failing (e.g. the app-specific password was revoked);
  * AI classification failing (e.g. a wrong key or no balance left).

Every alert is stored once in the alerts table, keyed by (kind, key), and e-mailed once;
the same problem is never repeated every half hour. A mailbox alert is cleared by the
next successful login, so a later failure is reported again. Alerts found during a
--no-send run stay pending and go out with the next run that sends.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape

from autobill.report.style import card_label
from autobill.store.db import now

REPORT_HEADER = "X-AutoBill-Report"  # alerts are ours too: never parsed as statements


@dataclass(frozen=True)
class Alert:
    kind: str  # unrecognized / failed / new_card / mailbox / ai
    key: str  # what makes it the same problem (Message-ID, account id, ...)
    title: str
    body: str


def unrecognized(message_id: str, subject: str, from_addr: str) -> Alert:
    return Alert(
        "unrecognized",
        message_id,
        "收到一封不认识的邮件",
        f"主题：{subject or '（无主题）'}\n发件人：{from_addr or '（未知）'}\n\n"
        "如果是新银行的账单：把这封邮件发给开发者加上这家银行的解析，"
        "之后运行 autobill run --rescan 就会补上（原件一直在 iCloud 里）。\n"
        "如果是银行的验证码、通知或广告，可以忽略。",
    )


def failed(message_id: str, subject: str, bank: str | None, error: str | None) -> Alert:
    return Alert(
        "failed",
        message_id,
        f"账单解析失败（{bank or '未知银行'}）",
        f"主题：{subject or '（无主题）'}\n原因：{error or '未知'}\n\n"
        "银行可能改了账单格式。原件保留在 iCloud 里，程序修好后运行 autobill run --rescan 会补上。",
    )


def new_card(account_id: str) -> Alert:
    return Alert(
        "new_card",
        account_id,
        f"发现新卡：{card_label(account_id)}",
        "如果是新办的卡，不用做什么，以后会自动算进报表。\n"
        f"如果是换卡（旧卡换了新卡号），在 config.yaml 的 cards.card_aliases 里加一行 "
        f'"{account_id}": "旧卡的账户"，新旧记录就会接上。',
    )


def mailbox(error: str) -> Alert:
    return Alert(
        "mailbox",
        "login",
        "收不到账单：登录邮箱失败",
        f"原因：{error}\n\n"
        "如果改过 Apple ID 密码，所有 App 专用密码都会失效：重新生成一个，填到运行 AutoBill 的"
        "机器上（服务器是 ~/.config/autobill.env）。修好之前，新账单会留在 iCloud 里，不会丢。",
    )


def ai(error: str) -> Alert:
    return Alert(
        "ai",
        "error",
        "AI 分类出错",
        f"原因：{error}\n\n"
        "常见原因是密钥填错或余额用完。修好之前，新商户先算“未分类”，账单和报表照常。"
        "修好后下一次运行会自动补上；同一个问题只提醒这一次。",
    )


def record(conn: sqlite3.Connection, alerts: list[Alert]) -> int:
    """Store new alerts; an alert already stored (same kind and key) is ignored."""
    added = 0
    for a in alerts:
        cur = conn.execute(
            "INSERT OR IGNORE INTO alerts (kind, key, title, body, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (a.kind, a.key, a.title, a.body, now()),
        )
        added += cur.rowcount
    return added


def clear(conn: sqlite3.Connection, kind: str, key: str) -> None:
    conn.execute("DELETE FROM alerts WHERE kind = ? AND key = ?", (kind, key))


def pending(conn: sqlite3.Connection) -> list[tuple[int, Alert]]:
    rows = conn.execute(
        "SELECT id, kind, key, title, body FROM alerts WHERE sent_at IS NULL ORDER BY id"
    )
    return [(r[0], Alert(r[1], r[2], r[3], r[4])) for r in rows]


def build_email(alerts: list[Alert], sender: str, to_addr: str) -> EmailMessage:
    """One e-mail for all new alerts of a run: plain text plus a small, clean HTML part."""
    first = alerts[0].title
    more = f" 等 {len(alerts)} 条" if len(alerts) > 1 else ""
    msg = EmailMessage()
    msg["Subject"] = f"⚠ AutoBill 提醒：{first}{more}"
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="autobill.invalid")
    msg[REPORT_HEADER] = "true"
    text = "\n\n".join(f"【{a.title}】\n{a.body}" for a in alerts)
    msg.set_content(text + "\n\n—— AutoBill 自动发送，同一个问题只提醒一次。")
    items = "".join(
        f'<div class="item"><div class="t">{escape(a.title)}</div>'
        f'<div class="b">{escape(a.body).replace(chr(10), "<br>")}</div></div>'
        for a in alerts
    )
    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="format-detection" content="telephone=no, date=no, address=no, email=no">'
        "<style>"
        ":root{--bg:#f2f2f7;--card:#fff;--label:#000;--label2:rgba(60,60,67,.6);"
        "--sep:rgba(60,60,67,.29);--warn:#c93400}"
        "@media (prefers-color-scheme: dark){:root{--bg:#000;--card:#1c1c1e;--label:#fff;"
        "--label2:rgba(235,235,245,.6);--sep:rgba(84,84,88,.65);--warn:#ff9f0a}}"
        "body{margin:0;background:var(--bg);color:var(--label);font:15px/1.45 -apple-system,"
        '"PingFang SC",sans-serif}.wrap{max-width:560px;margin:0 auto;padding:20px 16px}'
        "h1{font-size:28px;margin:6px 0 14px}.card{background:var(--card);border-radius:14px;"
        "overflow:hidden}.item{padding:14px 16px}.item+.item{border-top:.5px solid var(--sep)}"
        ".t{font-weight:600;color:var(--warn);margin-bottom:4px}.b{color:var(--label)}"
        "footer{font-size:12px;color:var(--label2);text-align:center;margin-top:18px}"
        '</style></head><body><div class="wrap"><h1>需要你看一下</h1>'
        f'<div class="card">{items}</div>'
        "<footer>AutoBill 自动发送 · 同一个问题只提醒一次</footer></div></body></html>"
    )
    msg.add_alternative(html, subtype="html")
    return msg


def send_pending(conn: sqlite3.Connection, mailer) -> int:
    """E-mail every pending alert in one message; mark them sent only on success."""
    rows = pending(conn)
    if not rows:
        return 0
    mailer.send(build_email([a for _, a in rows], mailer.config.username, mailer.config.to_addr))
    stamp = now()
    conn.executemany("UPDATE alerts SET sent_at = ? WHERE id = ?", [(stamp, i) for i, _ in rows])
    return len(rows)
