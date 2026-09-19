"""The export filter must be proven to hold back names before we trust what it prints.

Anything the default output prints is meant to be pasted into a public repository, so a
name that slips through cannot be taken back out of the history. Same reasoning as
test_check_identity.py.
"""

import sqlite3

import export_ai_categories as ex
import pytest
from export_ai_categories import EXPORT, HOLD, REVIEW, as_table, as_yaml, split, verdict_for

# Placeholder people's names, the same ones tests/fixtures/README.md uses.
PERSON = "李四"
PERSON3 = "张小明"


def db(rows: list[tuple], txn_merchants: list[str | None] | None = None) -> sqlite3.Connection:
    """An in-memory database holding `rows` as (merchant, category, confidence, location).

    Every merchant also gets a transaction unless `txn_merchants` says otherwise, since a
    merchant with no transaction of its own is a raw description.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE ai_categories (merchant TEXT PRIMARY KEY, category TEXT, guess TEXT,"
        " confidence TEXT NOT NULL, reason TEXT NOT NULL, searched INTEGER NOT NULL,"
        " location TEXT, currency TEXT, model TEXT NOT NULL, asked_at TEXT NOT NULL);"
        "CREATE TABLE transactions (merchant TEXT, description_raw TEXT NOT NULL);"
    )
    conn.executemany(
        "INSERT INTO ai_categories VALUES (?, ?, ?, ?, 'r', 0, ?, 'AUD', 'm', 't')",
        [(m, c, c, conf, loc) for m, c, conf, loc in rows],
    )
    names = [m for m, *_ in rows] if txn_merchants is None else txn_merchants
    conn.executemany("INSERT INTO transactions VALUES (?, 'raw')", [(n,) for n in names])
    return conn


def printed_by_default(safe) -> str:
    """Everything the script prints without --review or --show-held."""
    return "\n".join(as_table(safe) + as_yaml(safe))


def test_a_plain_shop_is_exported():
    conn = db([("STARBUCKS", "餐饮", "high", "SYDNEY AU")])
    safe, review, held = split(ex.rows_of(conn))
    assert [r["merchant"] for r in safe] == ["STARBUCKS"]
    assert (review, held) == ([], [])


@pytest.mark.parametrize(
    "merchant",
    [
        f"财付通-{PERSON}",
        f"支付宝-{PERSON}",
        f"微信支付-{PERSON}",
        f"转账-{PERSON}",
        f"个人收款-{PERSON}",
    ],
)
def test_payment_channel_names_are_held_back(merchant):
    """The whole point of the script: these must never reach the printed output."""
    conn = db([(merchant, "餐饮", "high", None)])
    safe, _, held = split(ex.rows_of(conn))
    assert safe == [], f"{merchant} 本该扣下来"
    assert len(held) == 1
    assert merchant not in printed_by_default(safe)


@pytest.mark.parametrize("merchant", [PERSON, PERSON3, "古茗", "海底捞"])
def test_bare_chinese_names_wait_for_the_author(merchant):
    """Two or three Chinese characters is the shape of a person AND of many chains, so
    these are neither exported nor dropped: the author sorts them with --review."""
    conn = db([(merchant, "餐饮", "high", None)])
    safe, review, held = split(ex.rows_of(conn))
    assert safe == [], f"{merchant} 不该自动公开"
    assert [r["merchant"] for r, _ in review] == [merchant]
    assert held == []
    assert merchant not in printed_by_default(safe)


def test_a_longer_chinese_shop_name_is_exported():
    """Four characters or more is a shop name, not the shape of a personal name."""
    conn = db([("永和豆浆", "餐饮", "high", None), ("沙县小吃", "餐饮", "high", None)])
    safe, review, _ = split(ex.rows_of(conn))
    assert {r["merchant"] for r in safe} == {"沙县小吃", "永和豆浆"}
    assert review == []


def test_a_merchant_with_no_transaction_is_a_raw_description():
    """`ai_categories.merchant` falls back to the raw description, which may carry a name."""
    conn = db([("SOMETHING ODD", "其他", "high", None)], txn_merchants=[])
    safe, _, held = split(ex.rows_of(conn))
    assert safe == []
    assert held[0][1].startswith("原始描述")


def test_a_raw_description_is_held_even_when_it_looks_like_a_shop():
    """The raw-description check comes first: it is the one that hides people's names."""
    assert verdict_for("STARBUCKS", parsed_count=0)[0] == HOLD
    assert verdict_for("STARBUCKS", parsed_count=1)[0] == EXPORT


def test_unsure_answers_are_not_exported():
    """category IS NULL means the AI was not sure; it counts as 其他, not a classification."""
    conn = db([("MYSTERY SHOP", None, "low", None)])
    safe, review, held = split(ex.rows_of(conn))
    assert (safe, review, held) == ([], [], [])


def test_an_unsure_answer_is_still_held_when_it_carries_a_name():
    """Held beats unsure, so --show-held shows everything risky the table skipped."""
    conn = db([(f"财付通-{PERSON}", None, "low", None)])
    _, _, held = split(ex.rows_of(conn))
    assert len(held) == 1


def test_a_chinese_chain_is_not_mistaken_for_a_person():
    for shop in ("永和豆浆", "沙县保健", "老乡鸡加盟店"):
        assert not ex.is_bare_chinese_name(shop), shop
    assert verdict_for("永和豆浆", parsed_count=3) == (EXPORT, "")
    assert verdict_for("古茗", parsed_count=3)[0] == REVIEW


def test_yaml_groups_by_category_and_keeps_names_readable():
    conn = db(
        [
            ("STARBUCKS", "餐饮", "high", "SYDNEY AU"),
            ("WOOLWORTHS", "超市", "high", "SYDNEY AU"),
            ("永和豆浆", "餐饮", "medium", None),
        ]
    )
    safe, _, _ = split(ex.rows_of(conn))
    text = "\n".join(as_yaml(safe))
    assert "餐饮:" in text and "超市:" in text
    assert text.count("餐饮:") == 1, "一个分类只出现一次"
    assert '- "STARBUCKS"' in text
    assert '- "永和豆浆"' in text  # not \\u escaped


def test_only_four_fields_leave_the_database():
    """Merchant, location, currency and confidence; never the model, the date or the reason."""
    conn = db([("STARBUCKS", "餐饮", "high", "SYDNEY AU")])
    conn.execute("UPDATE ai_categories SET reason = 'a coffee chain', asked_at = '2026-09-20'")
    safe, _, _ = split(ex.rows_of(conn))
    printed = printed_by_default(safe)
    assert "a coffee chain" not in printed
    assert "2026-09-20" not in printed
