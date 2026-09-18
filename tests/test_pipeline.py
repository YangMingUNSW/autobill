import re
import shutil
from email.message import EmailMessage
from pathlib import Path

import pytest

from autobill.fetch.source import DirectorySource, RawMail
from autobill.pipeline import process
from autobill.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(isolated_data_dir):
    return connect(isolated_data_dir / "autobill.db"), isolated_data_dir


def import_dir(conn, data_dir, path):
    return [process(conn, data_dir, mail) for mail in DirectorySource(path).iter_new()]


def dump(conn) -> dict[str, list[tuple]]:
    """Every row of every table, for before/after comparisons."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables
    }


def test_import_all_fixtures(env):
    conn, data_dir = env
    outcomes = {Path(o.source).name: o for o in import_dir(conn, data_dir, FIXTURES)}
    assert {n: o.status for n, o in outcomes.items()} == {
        "abc_mc_2026-09.eml": "OK",
        "abc_unionpay_2026-09.eml": "OK",
        "abc_visa_2026-09.eml": "OK",
        "boc_combined_2025-06.eml": "OK",  # two cards -> two bills
        "boc_visa_2026-08.eml": "OK",
        "ccb_visa_2026-06.eml": "OK",
        "ccb_visa_2026-07.eml": "OK",
    }
    assert conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 7
    assert len(list((data_dir / "raw").glob("*.eml"))) == 7  # originals kept for reparse


def test_importing_twice_changes_nothing(env):
    conn, data_dir = env
    import_dir(conn, data_dir, FIXTURES)
    before = dump(conn)
    second = import_dir(conn, data_dir, FIXTURES)
    assert {o.status for o in second} == {"SKIPPED"}
    assert dump(conn) == before


def test_same_statement_from_another_email_updates_the_bill(env, tmp_path):
    conn, data_dir = env
    import_dir(conn, data_dir, FIXTURES / "abc")
    # The same statement forwarded again: new Message-ID, same content.
    data = (FIXTURES / "abc" / "abc_mc_2026-09.eml").read_bytes()
    resent, n = re.subn(
        rb"(?mi)^Message-Id:[^\r\n]*", b"Message-ID: <resent@example.invalid>", data
    )
    assert n == 1
    outcome = process(conn, data_dir, RawMail(resent, "test:resent"))
    assert outcome.status == "OK"
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 3
    row = conn.execute(
        "SELECT source_message_id FROM bills WHERE account_id = 'ABC:0001'"
    ).fetchone()
    assert row[0] == "<resent@example.invalid>"
    txns = conn.execute(
        "SELECT COUNT(*) FROM transactions t JOIN bills b ON b.id = t.bill_id"
        " WHERE b.account_id = 'ABC:0001'"
    ).fetchone()[0]
    assert txns == 7  # replaced, not duplicated


def abc_lookalike(html: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "e-statement@creditcard.abchina.com.cn"
    msg["Subject"] = "中国农业银行金穗信用卡电子对账单"
    msg["Message-ID"] = "<broken@example.invalid>"
    msg["Date"] = "Wed, 02 Sep 2026 18:01:38 +0800"
    msg.set_content(html, subtype="html")
    return msg.as_bytes()


def test_template_change_is_failed_with_reason(env):
    conn, data_dir = env
    html = "<p>您的信用卡账户信息</p><p>账务说明</p><p>交易明细</p>"  # markers but no tables
    outcome = process(conn, data_dir, RawMail(abc_lookalike(html), "test:broken"))
    assert outcome.status == "FAILED" and "TemplateChanged" in outcome.error
    status, error = conn.execute("SELECT status, error FROM emails").fetchone()
    assert status == "FAILED" and "TemplateChanged" in error
    assert conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 0


def test_directory_source_reads_only_eml(tmp_path):
    shutil.copy(FIXTURES / "abc" / "abc_mc_2026-09.eml", tmp_path / "a.eml")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    shutil.copy(FIXTURES / "ccb" / "ccb_visa_2026-07.eml", tmp_path / "sub" / "b.eml")
    names = [Path(m.source).name for m in DirectorySource(tmp_path).iter_new()]
    assert names == ["a.eml", "b.eml"]


def test_directory_source_rejects_missing_dir(tmp_path):
    with pytest.raises(NotADirectoryError):
        DirectorySource(tmp_path / "nope")
