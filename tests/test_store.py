from datetime import datetime
from pathlib import Path

import pytest

from autobill.fetch.message import BEIJING, RawMessage
from autobill.parse.abc import AbcHtmlParser
from autobill.store.db import SCHEMA_VERSION, connect, load_bill, save_bill

ABC = sorted((Path(__file__).parent / "fixtures" / "abc").glob("*.eml"))


def parse(path: Path):
    (bill,) = AbcHtmlParser().parse(RawMessage.from_bytes(path.read_bytes()))
    return bill


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "test.db")


@pytest.mark.parametrize("path", ABC, ids=lambda p: p.stem)
def test_round_trip_is_exact(conn, path):
    bill = parse(path)
    bill_id = save_bill(conn, bill, None)
    assert load_bill(conn, bill_id) == bill


def test_money_is_stored_as_text(conn):
    save_bill(conn, parse(ABC[0]), None)
    types = {r[0] for r in conn.execute("SELECT DISTINCT typeof(amount) FROM transactions")}
    assert types == {"text"}
    assert conn.execute("SELECT amount FROM transactions LIMIT 1").fetchone()[0] == "371.53"


def test_same_statement_saved_twice_keeps_one_copy(conn):
    bill = parse(ABC[0])
    first = save_bill(conn, bill, None)
    second = save_bill(conn, bill, None)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 1
    count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count == len(bill.transactions)


def test_update_keeps_reported_at(conn):
    bill = parse(ABC[0])
    sent = datetime(2026, 9, 3, 8, 0, tzinfo=BEIJING)
    bill_id = save_bill(conn, bill.model_copy(update={"reported_at": sent}), None)
    save_bill(conn, bill, None)  # re-imported without reported_at
    assert load_bill(conn, bill_id).reported_at == sent


def test_foreign_keys_cascade(conn):
    bill_id = save_bill(conn, parse(ABC[0]), None)
    conn.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_refuses_newer_schema(tmp_path):
    path = tmp_path / "future.db"
    connect(path).execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError):
        connect(path)
