"""Processing one raw e-mail into the database. See docs/pipeline.md#状态机.

M3 covers the offline path (import-dir): store the original, recognise the bank, parse,
and write the e-mail row and its bills in one transaction.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from autobill.fetch.message import RawMessage
from autobill.fetch.source import RawMail
from autobill.model import Bill
from autobill.parse.base import TemplateChanged
from autobill.parse.registry import find_parser
from autobill.parse.util import FormatError
from autobill.store.db import now, save_bill


@dataclass
class Outcome:
    source: str
    status: str  # OK / WARN / UNVERIFIED / FAILED / UNRECOGNIZED / SKIPPED
    bank: str | None = None
    bills: list[Bill] = field(default_factory=list)
    error: str | None = None
    message_id: str = ""
    subject: str = ""
    from_addr: str = ""


def save_raw(data_dir: Path, msg: RawMessage) -> Path:
    """Keep the original e-mail as raw/<sha256>.eml (temp file + atomic rename)."""
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / f"{msg.sha256}.eml"
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(msg.raw)
        os.replace(tmp, target)
    return target


MULTI_CARD_WARNING = "这份账单里有多个卡号"


def apply_aliases(bill: Bill, aliases: dict[str, str]) -> Bill:
    """File the bill under its canonical account (config cards.card_aliases).

    When every card number on the bill belongs to that one account, the parser's
    "several card numbers" warning is expected, so it is dropped; a bill whose only
    warning it was becomes OK again.
    """
    if not aliases:
        return bill
    account = aliases.get(bill.account_id, bill.account_id)
    same = {aliases.get(f"{bill.bank}:{c}", f"{bill.bank}:{c}") for c in bill.cards} <= {account}
    warnings = [w for w in bill.warnings if not (same and w.startswith(MULTI_CARD_WARNING))]
    status = bill.status
    if status == "WARN" and not warnings:
        status = "OK"
    return bill.model_copy(update={"account_id": account, "warnings": warnings, "status": status})


def process(
    conn: sqlite3.Connection,
    data_dir: Path,
    mail: RawMail,
    aliases: dict[str, str] | None = None,
) -> Outcome:
    msg = RawMessage.from_bytes(mail.data)
    known = conn.execute(
        "SELECT id, status FROM emails WHERE message_id = ?", (msg.message_id,)
    ).fetchone()
    if known is not None:
        if known["status"] not in RETRY_STATUSES:
            return Outcome(mail.source, "SKIPPED", message_id=msg.message_id)  # e-mail dedupe
        # It failed or was not recognised before; the code may have been fixed since.
        conn.execute("DELETE FROM emails WHERE id = ?", (known["id"],))

    save_raw(data_dir, msg)
    parser, bills, status, error = _parse(msg, aliases)

    conn.execute("BEGIN")
    try:
        email_id = conn.execute(
            """INSERT INTO emails (message_id, sha256, source, subject, from_addr, sent_at, bank,
                                   status, error, first_seen_at, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.message_id,
                msg.sha256,
                mail.source,
                msg.subject,
                msg.from_addr,
                msg.sent_at.isoformat() if msg.sent_at else None,
                parser.bank if parser else None,
                status,
                error,
                now(),
                now(),
            ),
        ).lastrowid
        for bill in bills:
            save_bill(conn, bill, email_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    bank = parser.bank if parser else None
    return Outcome(
        mail.source, status, bank, bills, error, msg.message_id, msg.subject, msg.from_addr
    )


def _parse(msg: RawMessage, aliases: dict[str, str] | None):
    """(parser, bills, status, error) for one message; never a silent empty result."""
    parser = find_parser(msg)
    if parser is None:
        return None, [], "UNRECOGNIZED", None
    try:
        bills = parser.parse(msg)
        if not bills:  # parsers must raise instead; never accept a silent empty result
            raise TemplateChanged(f"{parser.name} returned no bills")
        bills = [apply_aliases(b, aliases or {}) for b in bills]
        return parser, bills, _worst([b.status for b in bills]), None
    except (TemplateChanged, FormatError) as exc:
        return parser, [], "FAILED", f"{type(exc).__name__}: {exc}"


def reparse(
    conn: sqlite3.Connection, data_dir: Path, email_id: int, aliases: dict[str, str] | None = None
) -> Outcome:
    """Parse a stored e-mail again from raw/<sha256>.eml, after the parser or the card
    aliases changed (docs/pipeline.md#cli). Its bills are updated in place: same rows,
    reported_at kept, so no report is sent again; bills the new parse no longer produces
    (e.g. filed under another account now) are removed."""
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    raw = data_dir / "raw" / f"{row['sha256']}.eml"
    if not raw.exists():
        return Outcome(
            row["source"], "SKIPPED", error="原始邮件文件不在了", message_id=row["message_id"]
        )
    msg = RawMessage.from_bytes(raw.read_bytes())
    parser, bills, status, error = _parse(msg, aliases)
    conn.execute("BEGIN")
    try:
        # Carry reported_at over to a bill that moved to another account (an alias).
        old = {
            (r["statement_date"]): r["reported_at"]
            for r in conn.execute(
                "SELECT statement_date, reported_at FROM bills WHERE email_id = ?", (email_id,)
            )
        }
        kept = []
        for bill in bills:
            reported = old.get(bill.statement_date.isoformat())
            if reported and bill.reported_at is None:
                bill = bill.model_copy(update={"reported_at": datetime.fromisoformat(reported)})
            kept.append(save_bill(conn, bill, email_id))
        marks = ",".join("?" * len(kept)) or "NULL"
        conn.execute(
            f"DELETE FROM bills WHERE email_id = ? AND id NOT IN ({marks})", [email_id, *kept]
        )
        conn.execute(
            "UPDATE emails SET status = ?, error = ?, bank = ?, processed_at = ? WHERE id = ?",
            (status, error, parser.bank if parser else None, now(), email_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    bank = parser.bank if parser else None
    return Outcome(
        row["source"], status, bank, bills, error, msg.message_id, msg.subject, msg.from_addr
    )


RETRY_STATUSES = {"FAILED", "UNRECOGNIZED"}  # met again: processed again, not skipped
_SEVERITY = ["OK", "UNVERIFIED", "WARN"]


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=_SEVERITY.index)


@dataclass
class SendResult:
    sent: list[int] = field(default_factory=list)  # bill ids now reported
    emails: int = 0
    failed: tuple[str, str] | None = None  # (statement month, error); the first failure stops


def send_pending_reports(
    conn: sqlite3.Connection,
    mailer,
    fx,
    rules=None,
    *,
    portfolio=(),
    attach=None,
    today=None,
) -> SendResult:
    """One progress e-mail per statement month with new statements (docs/notify.md#账单月进度邮件),
    oldest month first; plus the final e-mail of a month whose missing cards have run out
    of time without any new statement arriving.

    reported_at is written only after the mail server accepted the message, so a failed
    send is retried on the next run and a successful one is never repeated. The first
    failure stops the run: when the login or server is broken, every later send would
    fail the same way.
    """
    from autobill.report.cycle import build_cycle_email, open_cycles, record_sent

    result = SendResult()
    months: dict[str, list[int]] = {}
    for bill_id, statement_date in conn.execute(
        "SELECT id, statement_date FROM bills WHERE reported_at IS NULL ORDER BY statement_date, id"
    ):
        months.setdefault(statement_date[:7], []).append(bill_id)
    for cycle in open_cycles(conn):
        months.setdefault(cycle, [])

    for cycle, bill_ids in sorted(months.items()):
        try:
            message, report = build_cycle_email(
                conn,
                cycle,
                bill_ids,
                fx,
                mailer.config.username,
                mailer.config.to_addr,
                portfolio=portfolio,
                rules=rules,
                attach=attach,
                today=today,
            )
            if not bill_ids and not report.complete:
                continue  # an open month with nothing new: wait
            mailer.send(message)
        except Exception as exc:  # noqa: BLE001 - report the error, keep the bills pending
            result.failed = (cycle, f"{type(exc).__name__}: {exc}")
            break
        conn.execute("BEGIN")
        try:
            stamp = now()
            conn.executemany(
                "UPDATE bills SET reported_at = ? WHERE id = ?", [(stamp, i) for i in bill_ids]
            )
            record_sent(conn, cycle, message["Message-ID"], report.complete)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        result.sent += bill_ids
        result.emails += 1
    return result
