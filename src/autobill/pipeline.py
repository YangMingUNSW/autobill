"""Processing one raw e-mail into the database. See docs/pipeline.md#状态机.

M3 covers the offline path (import-dir): store the original, recognise the bank, parse,
and write the e-mail row and its bills in one transaction.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
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


def process(conn: sqlite3.Connection, data_dir: Path, mail: RawMail) -> Outcome:
    msg = RawMessage.from_bytes(mail.data)
    known = conn.execute(
        "SELECT status FROM emails WHERE message_id = ?", (msg.message_id,)
    ).fetchone()
    if known is not None:
        return Outcome(mail.source, "SKIPPED")  # e-mail-level dedupe: already processed

    save_raw(data_dir, msg)
    parser = find_parser(msg)
    bills: list[Bill] = []
    error = None
    if parser is None:
        status = "UNRECOGNIZED"
    else:
        try:
            bills = parser.parse(msg)
            if not bills:  # parsers must raise instead; never accept a silent empty result
                raise TemplateChanged(f"{parser.name} returned no bills")
            status = _worst([b.status for b in bills])
        except (TemplateChanged, FormatError) as exc:
            status, error = "FAILED", f"{type(exc).__name__}: {exc}"

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
    return Outcome(mail.source, status, parser.bank if parser else None, bills, error)


_SEVERITY = ["OK", "UNVERIFIED", "WARN"]


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=_SEVERITY.index)
