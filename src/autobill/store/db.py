"""SQLite storage. See docs/data-model.md#sqlite-表 and docs/pipeline.md.

Money is stored as TEXT ("28.25") and turned back into Decimal on read. Never SUM()
money in SQL: SQLite would convert it to floating point. Sum in Python instead.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from autobill.model import Bill, BillBalance, Transaction

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id            INTEGER PRIMARY KEY,
    message_id    TEXT NOT NULL UNIQUE,     -- falls back to "sha256:<hash>"
    sha256        TEXT NOT NULL,
    source        TEXT NOT NULL,            -- e.g. "dir:<path>"
    subject       TEXT,
    from_addr     TEXT,
    sent_at       TEXT,
    bank          TEXT,
    status        TEXT NOT NULL,            -- OK / WARN / UNVERIFIED / FAILED / UNRECOGNIZED
    error         TEXT,
    first_seen_at TEXT NOT NULL,
    processed_at  TEXT
);

CREATE TABLE IF NOT EXISTS bills (
    id                INTEGER PRIMARY KEY,
    bank              TEXT NOT NULL,
    account_id        TEXT NOT NULL,
    statement_date    TEXT NOT NULL,
    cards             TEXT NOT NULL,        -- JSON list
    period_start      TEXT,
    period_end        TEXT,
    due_date          TEXT,
    email_date        TEXT NOT NULL,
    status            TEXT NOT NULL,
    warnings          TEXT NOT NULL,        -- JSON list
    quality           INTEGER NOT NULL,
    reported_at       TEXT,
    email_id          INTEGER REFERENCES emails(id),
    source_message_id TEXT NOT NULL,
    source_sha256     TEXT NOT NULL,
    parser_name       TEXT NOT NULL,
    parser_version    INTEGER NOT NULL,
    UNIQUE (bank, account_id, statement_date)
);

CREATE TABLE IF NOT EXISTS bill_balances (
    bill_id          INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    currency         TEXT NOT NULL,
    previous_balance TEXT NOT NULL,
    previous_deposit TEXT NOT NULL,
    new_charges      TEXT NOT NULL,
    interest_fees    TEXT NOT NULL,
    payments_credits TEXT NOT NULL,
    adjustments      TEXT NOT NULL,
    amount_due       TEXT NOT NULL,
    deposit          TEXT NOT NULL,
    min_payment      TEXT,
    UNIQUE (bill_id, currency)
);

CREATE TABLE IF NOT EXISTS transactions (
    bill_id           INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    line_no           INTEGER NOT NULL,
    txn_id            TEXT NOT NULL,
    trans_date        TEXT NOT NULL,
    post_date         TEXT,
    txn_type          TEXT NOT NULL,
    amount            TEXT NOT NULL,
    currency          TEXT NOT NULL,
    orig_amount       TEXT,
    orig_currency     TEXT,
    fx_rate           TEXT,
    description_raw   TEXT NOT NULL,
    group_raw         TEXT,
    merchant          TEXT,
    merchant_location TEXT,
    card_last4        TEXT,
    installment       TEXT,
    category          TEXT,
    synthetic         INTEGER NOT NULL,
    UNIQUE (bill_id, line_no)
);
CREATE INDEX IF NOT EXISTS transactions_date ON transactions (trans_date);

CREATE TABLE IF NOT EXISTS fx_rates (
    date        TEXT NOT NULL,   -- the day asked for (a bill's email_date)
    currency    TEXT NOT NULL,
    rate_to_cny TEXT NOT NULL,
    rate_date   TEXT NOT NULL,   -- the day the rate was published (weekends: earlier)
    source      TEXT NOT NULL,   -- "frankfurter"; config fallbacks are never cached
    fetched_at  TEXT NOT NULL,
    UNIQUE (date, currency)
);
"""

# Schema changes after the first release; a new database gets SCHEMA and all of these.
MIGRATIONS = {
    2: """
CREATE TABLE IF NOT EXISTS cycle_threads (
    cycle        TEXT PRIMARY KEY,         -- statement month, "2026-09"
    message_ids  TEXT NOT NULL,            -- JSON list of the progress e-mails sent, oldest first
    completed_at TEXT,                     -- set when the latest e-mail had every card settled
    updated_at   TEXT NOT NULL
);
""",
    3: """
CREATE TABLE IF NOT EXISTS folder_cursors (
    folder      TEXT PRIMARY KEY,          -- IMAP folder name
    uidvalidity INTEGER NOT NULL,          -- when it changes, UIDs were renumbered: rescan
    last_uid    INTEGER NOT NULL,          -- every message up to this UID has been processed
    updated_at  TEXT NOT NULL
);
""",
}

BALANCE_FIELDS = [f for f in BillBalance.model_fields if f != "currency"]
TXN_FIELDS = list(Transaction.model_fields)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and create or migrate) the database."""
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions only
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {version} is newer than this program ({SCHEMA_VERSION})"
        )
    if version == 0:
        conn.executescript(SCHEMA)
    for step in range(max(version, 1) + 1, SCHEMA_VERSION + 1):
        conn.executescript(MIGRATIONS[step])
    if version != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _text(value):
    """Python value -> SQLite value: Decimal as exact text, dates ISO, bools 0/1."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


def save_bill(conn: sqlite3.Connection, bill: Bill, email_id: int | None) -> int:
    """Insert or replace one bill; the caller owns the transaction.

    One row per (bank, account_id, statement_date): a statement arriving again through
    another e-mail updates the same row, and its balances and transactions are replaced
    as a whole (docs/pipeline.md#三层去重). reported_at survives the update.
    """
    row = conn.execute(
        "SELECT id, reported_at FROM bills"
        " WHERE bank = ? AND account_id = ? AND statement_date = ?",
        (bill.bank, bill.account_id, bill.statement_date.isoformat()),
    ).fetchone()
    values = {
        "bank": bill.bank,
        "account_id": bill.account_id,
        "statement_date": bill.statement_date.isoformat(),
        "cards": json.dumps(bill.cards),
        "period_start": _text(bill.period_start),
        "period_end": _text(bill.period_end),
        "due_date": _text(bill.due_date),
        "email_date": bill.email_date.isoformat(),
        "status": bill.status,
        "warnings": json.dumps(bill.warnings, ensure_ascii=False),
        "quality": bill.quality,
        "reported_at": bill.reported_at.isoformat() if bill.reported_at else None,
        "email_id": email_id,
        "source_message_id": bill.source_message_id,
        "source_sha256": bill.source_sha256,
        "parser_name": bill.parser_name,
        "parser_version": bill.parser_version,
    }
    if row is None:
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        bill_id = conn.execute(
            f"INSERT INTO bills ({cols}) VALUES ({marks})", list(values.values())
        ).lastrowid
    else:
        bill_id = row["id"]
        values["reported_at"] = values["reported_at"] or row["reported_at"]
        sets = ", ".join(f"{c} = ?" for c in values)
        conn.execute(f"UPDATE bills SET {sets} WHERE id = ?", [*values.values(), bill_id])
        conn.execute("DELETE FROM bill_balances WHERE bill_id = ?", (bill_id,))
        conn.execute("DELETE FROM transactions WHERE bill_id = ?", (bill_id,))

    for b in bill.balances:
        cols = ["bill_id", "currency", *BALANCE_FIELDS]
        conn.execute(
            f"INSERT INTO bill_balances ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [bill_id, b.currency, *(_text(getattr(b, f)) for f in BALANCE_FIELDS)],
        )
    for t in bill.transactions:
        cols = ["bill_id", *TXN_FIELDS]
        conn.execute(
            f"INSERT INTO transactions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [bill_id, *(_text(getattr(t, f)) for f in TXN_FIELDS)],
        )
    return bill_id


_MONEY_TXN = {"amount", "orig_amount", "fx_rate"}
_DATE_TXN = {"trans_date", "post_date"}


def load_bill(conn: sqlite3.Connection, bill_id: int) -> Bill:
    """Read a bill back exactly as it was saved."""
    row = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if row is None:
        raise KeyError(bill_id)
    balances = [
        BillBalance(
            currency=r["currency"],
            **{f: Decimal(r[f]) if r[f] is not None else None for f in BALANCE_FIELDS},
        )
        for r in conn.execute(
            "SELECT * FROM bill_balances WHERE bill_id = ? ORDER BY rowid", (bill_id,)
        )
    ]
    transactions = []
    for r in conn.execute(
        "SELECT * FROM transactions WHERE bill_id = ? ORDER BY line_no", (bill_id,)
    ):
        fields = {}
        for f in TXN_FIELDS:
            v = r[f]
            if v is not None and f in _MONEY_TXN:
                v = Decimal(v)
            elif v is not None and f in _DATE_TXN:
                v = date.fromisoformat(v)
            elif f == "synthetic":
                v = bool(v)
            fields[f] = v
        transactions.append(Transaction(**fields))

    def d(v):
        return date.fromisoformat(v) if v else None

    return Bill(
        bank=row["bank"],
        account_id=row["account_id"],
        cards=json.loads(row["cards"]),
        statement_date=date.fromisoformat(row["statement_date"]),
        period_start=d(row["period_start"]),
        period_end=d(row["period_end"]),
        due_date=d(row["due_date"]),
        email_date=date.fromisoformat(row["email_date"]),
        balances=balances,
        transactions=transactions,
        status=row["status"],
        warnings=json.loads(row["warnings"]),
        quality=row["quality"],
        reported_at=datetime.fromisoformat(row["reported_at"]) if row["reported_at"] else None,
        source_message_id=row["source_message_id"],
        source_sha256=row["source_sha256"],
        parser_name=row["parser_name"],
        parser_version=row["parser_version"],
    )
