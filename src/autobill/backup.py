"""A copy of the database, small enough to travel in the month's e-mail.

Statements can be parsed again from raw/, and the AI can be asked again, but only if the
database survives; it is the one thing here that cannot be rebuilt from somewhere else.
So the month's e-mail carries a compressed copy of it (about 120 KB against a 490 KB
database), and the server keeps the last few next to it. The author's mailbox is then the
off-site copy: if the server disappears, the newest monthly e-mail still has everything.

The copy is made with SQLite's own backup, never by copying the file: in WAL mode the
newest changes live in autobill.db-wal, and a plain copy of autobill.db leaves them behind
(which is exactly what happened on 2026-09-20 during a manual backup).

The copy is taken while the month's e-mail is being built, so it does not yet know that
this month was reported. Restoring it therefore sends that month's e-mail once more -
harmless, and better than the other way round.
"""

from __future__ import annotations

import gzip
import sqlite3
import tempfile
from pathlib import Path

PREFIX = "autobill-"
SUFFIX = ".db.gz"


def database_path(conn: sqlite3.Connection) -> Path | None:
    """Where this connection's database lives; None for an in-memory one (tests)."""
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return Path(file) if file else None
    return None


def dump(conn: sqlite3.Connection) -> bytes:
    """The whole database as gzip bytes, consistent even while `serve` is writing."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "autobill.db"
        copy = sqlite3.connect(target)
        try:
            conn.backup(copy)
        finally:
            copy.close()
        return gzip.compress(target.read_bytes(), 6)


def write(data: bytes, folder: Path, cycle: str, keep: int) -> Path:
    """Keep this month's copy on the server too, and drop the oldest beyond `keep`."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{PREFIX}{cycle}{SUFFIX}"
    target.write_bytes(data)
    stale = sorted(folder.glob(f"{PREFIX}*{SUFFIX}"))[: -keep or None]
    for old in stale:
        old.unlink()
    return target


def name_for(cycle: str) -> str:
    return f"{PREFIX}{cycle}{SUFFIX}"
