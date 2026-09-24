"""The monthly backup (docs/notify.md#每月备份). A backup is only worth as much as its
restore, so these tests open the copy and read it, never just check that a file exists."""

import gzip
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fakes import FakeFrankfurter, FakeSMTP

from autobill import backup
from autobill.config import BackupConfig, FxConfig, SmtpReportConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.notify.mail import Mailer
from autobill.pipeline import process, send_pending_reports
from autobill.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109",
         ("AUD", "2026-08-24"): "4.6200", ("AUD", "2025-06-25"): "4.7000"}  # fmt: skip
SMTP = SmtpReportConfig(
    enabled=True, smtp_server="smtp.example.invalid",
    username="bills@example.invalid", to_addr="me@example.invalid",
)  # fmt: skip


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.instances.clear()


@pytest.fixture
def db(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    for mail in DirectorySource(FIXTURES / "abc").iter_new():
        process(conn, isolated_data_dir, mail)
    return conn, FxRates(conn, FxConfig(), FakeFrankfurter(RATES))


def send(conn, fx, config=None):
    return send_pending_reports(
        conn, Mailer(SMTP, "secret", FakeSMTP), fx,
        today=date(2026, 9, 30), backup=config if config is not None else BackupConfig(),
    )  # fmt: skip


def attachments(kind="application/gzip"):
    return [
        part
        for smtp in FakeSMTP.instances
        for message in smtp.sent
        for part in message.iter_attachments()
        if part.get_content_type() == kind
    ]


def test_the_month_carries_a_copy_of_the_database_that_opens(db, tmp_path):
    """The point of the whole thing: the attachment must restore to a working database."""
    conn, fx = db
    bills = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    send(conn, fx)
    (part,) = attachments()
    assert part.get_filename() == "autobill-2026-09.db.gz"
    restored = tmp_path / "autobill.db"
    restored.write_bytes(gzip.decompress(part.get_content()))
    copy = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == bills
    assert copy.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] > 0


def test_a_copy_stays_on_the_server_too(db, isolated_data_dir):
    conn, fx = db
    send(conn, fx)
    kept = list((isolated_data_dir / "backups").glob("*.db.gz"))
    assert [p.name for p in kept] == ["autobill-2026-09.db.gz"]
    assert gzip.decompress(kept[0].read_bytes())[:15] == b"SQLite format 3"


def test_only_the_newest_copies_are_kept(isolated_data_dir):
    folder = isolated_data_dir / "backups"
    for month in ("2026-05", "2026-06", "2026-07"):
        backup.write(b"x", folder, month, keep=2)
    assert sorted(p.name for p in folder.glob("*.gz")) == [
        "autobill-2026-06.db.gz",
        "autobill-2026-07.db.gz",
    ]


def test_the_copy_catches_writes_that_are_still_in_the_wal(db, tmp_path):
    """A plain file copy would miss these: in WAL mode the newest rows are not in
    autobill.db yet. This is the mistake the author's first manual backup made."""
    conn, _ = db
    conn.execute("BEGIN")
    conn.execute("INSERT INTO cycle_threads VALUES ('2099-01', '[]', NULL, 'now')")
    conn.execute("COMMIT")
    restored = tmp_path / "copy.db"
    restored.write_bytes(gzip.decompress(backup.dump(conn)))
    copy = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
    assert copy.execute("SELECT COUNT(*) FROM cycle_threads WHERE cycle='2099-01'").fetchone()[0]


def test_backup_can_be_turned_off(db, isolated_data_dir):
    conn, fx = db
    send(conn, fx, BackupConfig(enabled=False))
    assert attachments() == []
    assert not (isolated_data_dir / "backups").exists()


@pytest.mark.parametrize("step", ["dump", "write"])
def test_a_failed_backup_never_costs_the_report(db, monkeypatch, step):
    """Say the server's backups folder is not writable: the month's e-mail still goes out
    and counts as sent, and the error is handed back for the log."""
    conn, fx = db

    def broken(*args, **kwargs):
        raise PermissionError("backups folder is not writable")

    monkeypatch.setattr(backup, step, broken)
    result = send(conn, fx)
    assert result.emails == 1 and result.failed is None
    assert result.backup_errors == [("2026-09", "PermissionError: backups folder is not writable")]
    assert conn.execute("SELECT COUNT(*) FROM bills WHERE reported_at IS NULL").fetchone()[0] == 0


def test_no_backup_is_asked_for_by_default(db, isolated_data_dir):
    """send_pending_reports without a backup config (preview, tests) attaches nothing."""
    conn, fx = db
    send_pending_reports(conn, Mailer(SMTP, "secret", FakeSMTP), fx, today=date(2026, 9, 30))
    assert attachments() == [] and not (isolated_data_dir / "backups").exists()
