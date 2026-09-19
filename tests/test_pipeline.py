import re
import shutil
from email.message import EmailMessage
from pathlib import Path

import pytest

from autobill.fetch.message import RawMessage
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


# --- card aliases (config cards.card_aliases) -----------------------------------------


def _ccb_bill(cards, warnings, status="WARN"):
    from autobill.parse.registry import find_parser

    path = FIXTURES / "ccb" / "ccb_visa_2026-07.eml"
    msg = RawMessage.from_bytes(path.read_bytes())
    (bill,) = find_parser(msg).parse(msg)
    return bill.model_copy(update={"cards": cards, "warnings": warnings, "status": status,
                                   "account_id": f"CCB:{cards[0]}"})  # fmt: skip


def test_alias_files_a_two_card_statement_under_one_account():
    """The author's CCB 2025-06 statement lists two card numbers of one account (a UnionPay
    and an overseas card); config names the canonical one."""
    from autobill.pipeline import apply_aliases

    bill = _ccb_bill(["0009", "0004"], ["这份账单里有多个卡号 ['0009', '0004']，账户取 0009"])
    out = apply_aliases(bill, {"CCB:0009": "CCB:0004"})
    assert out.account_id == "CCB:0004" and out.cards == ["0009", "0004"]
    assert out.warnings == [] and out.status == "OK"


def test_alias_keeps_other_warnings_and_unrelated_cards():
    from autobill.pipeline import apply_aliases

    multi = "这份账单里有多个卡号 ['0009', '0004']，账户取 0009"
    bill = _ccb_bill(["0009", "0004"], [multi, "对账：差 1.00"])
    out = apply_aliases(bill, {"CCB:0009": "CCB:0004"})
    assert out.warnings == ["对账：差 1.00"] and out.status == "WARN"
    # a third, unaliased card: the multi-card warning must stay
    bill = _ccb_bill(["1111", "0009", "0004"], [multi])
    out = apply_aliases(bill, {"CCB:0009": "CCB:0004"})
    assert out.warnings == [multi] and out.status == "WARN"
    assert apply_aliases(bill, {}) is bill


def test_import_dir_uses_aliases_from_config(isolated_data_dir):
    """End to end: the alias in config.yaml decides the stored account."""
    from typer.testing import CliRunner

    from autobill.cli import app

    (isolated_data_dir / "config.yaml").write_text(
        'cards:\n  card_aliases:\n    "CCB:0004": "CCB:9999"\n', encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["import-dir", "--no-send", str(FIXTURES / "ccb")])
    assert result.exit_code == 0, result.output
    assert "CCB:9999" in result.output and "CCB:0004" not in result.output


# --- reparse: stored e-mails parsed again after a fix ------------------------------------


def _import_one(conn, data_dir, name="abc_mc_2026-09.eml"):
    data = (FIXTURES / "abc" / name).read_bytes()
    return process(conn, data_dir, RawMail(data, f"test:{name}"))


def test_reparse_updates_the_same_bill_and_keeps_reported_at(env):
    from autobill.pipeline import reparse

    conn, data_dir = env
    first = _import_one(conn, data_dir)
    (bill_id,) = [r[0] for r in conn.execute("SELECT id FROM bills")]
    # as if an older parser had left it WARN, and its report had been sent
    conn.execute(
        "UPDATE bills SET status = 'WARN', warnings = '[\"未知的交易类型\"]',"
        " reported_at = '2026-09-19T20:15:00+10:00'"
    )
    conn.execute("UPDATE emails SET status = 'WARN'")
    email_id = conn.execute("SELECT id FROM emails").fetchone()[0]
    out = reparse(conn, data_dir, email_id)
    assert out.status == first.status == "OK"
    row = conn.execute("SELECT id, status, warnings, reported_at FROM bills").fetchone()
    assert row[0] == bill_id and row[1] == "OK" and row[2] == "[]"
    assert row[3] == "2026-09-19T20:15:00+10:00"  # no second report
    assert conn.execute("SELECT status FROM emails").fetchone()[0] == "OK"


def test_reparse_moves_a_bill_to_its_aliased_account(env):
    from autobill.pipeline import reparse

    conn, data_dir = env
    _import_one(conn, data_dir)
    (old_account,) = [r[0] for r in conn.execute("SELECT account_id FROM bills")]
    conn.execute("UPDATE bills SET reported_at = '2026-09-19T20:15:00+10:00'")
    email_id = conn.execute("SELECT id FROM emails").fetchone()[0]
    reparse(conn, data_dir, email_id, {old_account: "ABC:9999"})
    rows = conn.execute("SELECT account_id, reported_at FROM bills").fetchall()
    assert [tuple(r) for r in rows] == [("ABC:9999", "2026-09-19T20:15:00+10:00")]


def test_reparse_without_the_raw_file_is_skipped(env):
    from autobill.pipeline import reparse

    conn, data_dir = env
    _import_one(conn, data_dir)
    for f in (data_dir / "raw").glob("*.eml"):
        f.unlink()
    email_id = conn.execute("SELECT id FROM emails").fetchone()[0]
    out = reparse(conn, data_dir, email_id)
    assert out.status == "SKIPPED" and conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 1


def test_reparse_command_only_touches_warn_and_failed_by_default(isolated_data_dir):
    from typer.testing import CliRunner

    from autobill.cli import app

    runner = CliRunner()
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES / "abc")])
    assert "没有需要重新解析" in runner.invoke(app, ["reparse"]).output  # all OK
    conn = connect(isolated_data_dir / "autobill.db")
    conn.execute("UPDATE emails SET status = 'WARN' WHERE id = 1")
    result = runner.invoke(app, ["reparse"])
    assert result.exit_code == 0 and "重新解析了 1 封：OK 1" in result.output
    result = runner.invoke(app, ["reparse", "--all"])
    assert "重新解析了 3 封：OK 3" in result.output
