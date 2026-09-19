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


def db(
    rows: list[tuple],
    txn_merchants: list[str | None] | None = None,
    descriptions: list[str] | None = None,
) -> sqlite3.Connection:
    """An in-memory database holding `rows` as (merchant, category, confidence, location).

    Every merchant also gets a transaction unless `txn_merchants` says otherwise, since a
    merchant with no transaction of its own is a raw description. `descriptions` sets the
    transactions' raw descriptions, which is how a stored merchant is recognised as being
    a whole description (the parser stores one there when it cannot split the merchant out).
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
    raws = descriptions if descriptions is not None else ["raw"] * len(names)
    conn.executemany("INSERT INTO transactions VALUES (?, ?)", list(zip(names, raws, strict=True)))
    return conn


def printed_by_default(safe) -> str:
    """Everything the script prints without --review or --show-held."""
    return "\n".join(as_table(safe) + as_yaml(safe))


def test_a_plain_shop_is_exported():
    conn = db([("STARBUCKS", "餐饮", "high", "SYDNEY AU")])
    safe, review, held = split(ex.rows_of(conn))
    assert [e.name for e in safe] == ["STARBUCKS"]
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
    assert {e.name for e in safe} == {"沙县小吃", "永和豆浆"}
    assert review == []


def test_a_merchant_with_no_transaction_is_a_raw_description():
    """`ai_categories.merchant` falls back to the raw description, which may carry a name."""
    conn = db([("SOMETHING ODD", "其他", "high", None)], txn_merchants=[])
    safe, _, held = split(ex.rows_of(conn))
    assert safe == []
    assert held[0][1].startswith("没有流水")


def test_a_raw_description_is_held_even_when_it_looks_like_a_shop():
    """The raw-description check comes first: it is the one that hides people's names."""
    assert verdict_for("STARBUCKS", parsed_count=0)[0] == HOLD
    assert verdict_for("STARBUCKS", parsed_count=1)[0] == EXPORT


def test_a_chinese_description_stored_as_the_merchant_waits_for_the_author():
    """The parser stores the whole description in `merchant` when it cannot split a shop
    out (195 of 1,807 transactions on the author's data), so "no transaction has this
    merchant" does not catch those. A Chinese one could have a person's name in it."""
    conn = db(
        [("网上消费 某某商户", "餐饮", "high", None)],
        descriptions=["网上消费 某某商户"],
    )
    safe, review, held = split(ex.rows_of(conn))
    assert safe == [] and held == []
    assert [r["merchant"] for r, _ in review] == ["网上消费 某某商户"]
    assert "原始描述" in review[0][1]


def test_a_latin_description_stored_as_the_merchant_is_still_exported():
    """Foreign card transactions print the shop, not a person, so holding every stored
    description back would cost most of the export for nothing."""
    conn = db(
        [("跨行消费 FAROS BROS PTY LTD MARRICKVILLEAUS", "超市", "high", None)],
        descriptions=["跨行消费 FAROS BROS PTY LTD MARRICKVILLEAUS"],
    )
    safe, _, _ = split(ex.rows_of(conn))
    assert [e.name for e in safe] == ["FAROS BROS PTY LTD MARRICKVILLEAUS"]


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


# --- cleaning the bank's payment markers off the name ---------------------------------


@pytest.mark.parametrize(
    ("stored", "cleaned"),
    [
        ("跨行消费 FAROS BROS PTY LTD MARRICKVILLEAUS", "FAROS BROS PTY LTD MARRICKVILLEAUS"),
        ("FAROS BROS PTY LTDAUSVISA Apple Pay", "FAROS BROS PTY LTD"),
        ("EBEST PTY LTDAUSVISAApple Pay", "EBEST PTY LTD"),
        ("境外消费 STARBUCKS PARIS", "STARBUCKS PARIS"),
        ("JPN 跨境消费 TORIYA UMEBOSI OSAKA JPN", "TORIYA UMEBOSI OSAKA JPN"),
        ("VISA OFFICE", "VISA OFFICE"),  # not a payment marker
    ],
)
def test_clean_name_drops_the_banks_markers(stored, cleaned):
    assert ex.clean_name(stored) == cleaned


def test_spellings_of_one_shop_become_one_line():
    """Three stored spellings of the same shop are one rule, not three."""
    conn = db(
        [
            ("FAROS BROS PTY LTD", "超市", "high", "MARRICKVILLE AU"),
            ("FAROS BROS PTY LTDAUSVISA Apple Pay", "超市", "medium", None),
            ("跨行消费 FAROS BROS PTY LTD", "超市", "high", None),
        ]
    )
    safe, _, _ = split(ex.rows_of(conn))
    assert [(e.name, e.variants) for e in safe] == [("FAROS BROS PTY LTD", 3)]
    assert "覆盖 3 种写法" in printed_by_default(safe)


def test_cleaning_never_turns_a_held_name_into_an_exported_one():
    """Stripping a prefix can only uncover a name, so the stricter verdict has to win."""
    conn = db([(f"网上消费 财付通-{PERSON}", "餐饮", "high", None)])
    safe, _, held = split(ex.rows_of(conn))
    assert safe == [] and len(held) == 1
    assert PERSON not in printed_by_default(safe)


def test_cleaning_can_uncover_a_bare_personal_name():
    """ "网上消费 李四" cleans to "李四", which is for the author to judge, not to publish."""
    conn = db([(f"网上消费 {PERSON}", "餐饮", "high", None)])
    safe, review, _ = split(ex.rows_of(conn))
    assert safe == [] and len(review) == 1
    assert PERSON not in printed_by_default(safe)


def test_a_transaction_number_is_flagged_but_not_cut_off():
    """Cutting "TOTAL 4375372" down to "TOTAL" would make exactly the kind of ambiguous
    keyword the rules deliberately leave out, so the author decides."""
    conn = db([("TOTAL 4375372", "交通", "high", "FR")])
    safe, _, _ = split(ex.rows_of(conn))
    assert [e.name for e in safe] == ["TOTAL 4375372"] and safe[0].noisy
    assert "建议改短" in printed_by_default(safe)


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
