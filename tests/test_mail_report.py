"""The report e-mail (docs/notify.md#邮件内容), built from the imported samples, offline."""

from decimal import Decimal
from pathlib import Path

import pytest
from fakes import FakeFrankfurter

from autobill.categorize import load_rules
from autobill.config import FxConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.pipeline import process
from autobill.report.mail_report import (
    COLORS,
    MonthSection,
    bar_rows,
    bill_digest,
    build_email,
    due_rows,
    expected_accounts,
    missing_accounts,
    months_for,
    preview_html,
)
from autobill.report.monthly import MonthlySummary
from autobill.store.db import connect, load_bill

FIXTURES = Path(__file__).parent / "fixtures"
D = Decimal
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109",
         ("AUD", "2026-08-24"): "4.6200", ("AUD", "2025-06-25"): "4.7000"}  # fmt: skip


@pytest.fixture
def db(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    for mail in DirectorySource(FIXTURES).iter_new():
        process(conn, isolated_data_dir, mail)
    return conn, FxRates(conn, FxConfig(), FakeFrankfurter(RATES))


def bill_id(conn, account, statement_date=None):
    query, args = "SELECT id FROM bills WHERE account_id = ?", [account]
    if statement_date:
        query += " AND statement_date = ?"
        args.append(statement_date)
    return conn.execute(query + " ORDER BY statement_date DESC", args).fetchone()[0]


def test_bill_digest(db):
    conn, fx = db
    bill = load_bill(conn, bill_id(conn, "ABC:0003"))
    digest = bill_digest(bill, fx, load_rules())
    assert digest.amounts_due == {"CNY": D("1217.97")} and digest.due_cny == D("1217.97")
    # 460.00 of 财付通 purchases + 99.59 instalment interest; the 659.00 principal is not spending
    assert digest.spend_cny == D("559.59")
    assert digest.top_categories == [("微信/支付宝（未细分）", D("460.00")), ("利息", D("99.59"))]
    assert str(digest.due_date) == "2026-10-05" and digest.status_note == ""
    assert digest.due_text == "¥ 1,217.97"


def test_months_for_covers_period_and_transactions(db):
    conn, _ = db
    assert months_for(load_bill(conn, bill_id(conn, "ABC:0003"))) == ["2026-08", "2026-09"]
    # BOC prints no period: the transaction months are used
    boc = load_bill(conn, bill_id(conn, "BOC:0005", "2025-06-22"))
    assert months_for(boc) == ["2025-05", "2025-06"]


def test_missing_cards(db):
    conn, _ = db
    assert set(expected_accounts(conn, "2026-08")) == {
        "ABC:0001", "ABC:0002", "ABC:0003", "BOC:0005", "CCB:0004",
    }  # fmt: skip
    # CCB's last bill ends 2026-07-10 and BOC's 2026-08-22: August is not covered yet.
    assert missing_accounts(conn, "2026-08") == ["中国银行 0005", "建设银行 0004"]
    # The combined BOC statement closes 2025-06-22: June needs the July statements. The
    # 2026-08 statement of card 0005 is far later and must not count as covering June.
    assert missing_accounts(conn, "2025-06") == ["中国银行 0005", "中国银行 0006"]


def test_due_rows_show_dates_and_amounts_only(db):
    conn, _ = db
    rows = {r.name: r for r in due_rows(conn, "2026-08")}
    assert (rows["农业银行 0003"].amount, rows["农业银行 0003"].note) == (
        "CNY 1,217.97",
        "09-16 账单 · 10-05 前还款",
    )
    assert rows["中国银行 0005"].amount == "无需还款"


def test_email_structure(db):
    conn, fx = db
    msg = build_email(
        conn, bill_id(conn, "ABC:0003"), fx, "from@example.invalid", "to@example.invalid"
    )
    assert msg["Subject"] == "信用卡账单汇总｜农业银行 0003｜2026-09-16"
    assert msg["X-AutoBill-Report"] == "true"
    assert (msg["From"], msg["To"]) == ("from@example.invalid", "to@example.invalid")
    assert "本期支出：¥ 559.59" in msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert "2026 年 8 月" in html and "2026 年 9 月" in html  # one section per month


def test_email_is_complete_without_pictures_links_or_single_transactions(db):
    conn, fx = db
    msg = build_email(conn, bill_id(conn, "ABC:0003"), fx, "a@example.invalid", "b@example.invalid")
    html = msg.get_body(("html",)).get_content()
    # QQ/NetEase Mail hide pictures by default: every chart is an HTML bar, no images
    assert [p for p in msg.walk() if p.get_content_maintype() == "image"] == []
    assert "<img" not in html and f"background:{COLORS['accent']}" in html
    assert "href=" not in html and "https://" not in html  # no links, no remote fonts
    assert "260831" not in html and "2026-08-31" not in html  # no per-transaction dates
    assert "还缺：中国银行 0005、建设银行 0004" in html
    assert "还款提醒" not in html and "请尽快还款" not in html
    assert 'style="' in html  # styles are inline for mail clients


def test_warn_bill_is_marked(db):
    conn, fx = db
    conn.execute("UPDATE bills SET status = 'WARN' WHERE account_id = 'ABC:0003'")
    msg = build_email(conn, bill_id(conn, "ABC:0003"), fx, "a@example.invalid", "b@example.invalid")
    assert "对账有警告" in msg.get_body(("html",)).get_content()


def test_preview_is_the_email_html(db):
    conn, fx = db
    html = preview_html(conn, bill_id(conn, "BOC:0006"), fx)
    assert "中国银行 · 0006" in html and "<img" not in html


def test_small_shares_never_read_zero_percent():
    rows = bar_rows([("大", D("9900")), ("小", D("30"))], total=D("9930"))
    assert [r.note for r in rows] == ["100%", "<1%"]
    assert rows[1].width == 2  # the smallest positive bar is still visible


def section(categories=None, missing=(), previous=None):
    summary = MonthlySummary("2026-08", categories=categories or {})
    return MonthSection("2026-08", summary, list(missing), [], [], previous)


def test_categories_fold_the_tail_and_put_uncategorised_last_in_grey():
    categories = {"未分类": D("900"), **{f"类{i}": D(100 - i) for i in range(8)}}
    rows = section(categories).category_rows
    assert [r.name for r in rows] == ["类0", "类1", "类2", "类3", "类4", "其他", "未分类"]
    assert rows[5].amount == "282.00"  # 类5 + 类6 + 类7 = 95 + 94 + 93
    assert rows[-1].color == COLORS["context"] and rows[0].color == ""


def test_no_month_on_month_comparison_while_cards_are_missing():
    assert "没到齐" in section(missing=["建设银行 0004"], previous=D("100")).delta_text
    assert "没有数据" in section().delta_text
