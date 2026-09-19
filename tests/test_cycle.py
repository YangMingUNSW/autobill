"""The progress e-mail of a statement month (docs/notify.md#账单月进度邮件), offline."""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fakes import FakeFrankfurter, FakeSMTP

from autobill.categorize import load_rules
from autobill.config import FxConfig, PortfolioCard, SmtpReportConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.notify.mail import Mailer
from autobill.pipeline import process, send_pending_reports
from autobill.report.cycle import (
    build_cycle_email,
    build_cycle_report,
    cycle_title,
    expected_cards,
    open_cycles,
    thread_ids,
)
from autobill.report.style import COLORS, bar_rows, category_bar_rows
from autobill.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
D = Decimal
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109",
         ("AUD", "2026-08-24"): "4.6200", ("AUD", "2025-06-25"): "4.7000"}  # fmt: skip
PORTFOLIO = [
    PortfolioCard(account="ABC:0001", statement_day=1),
    PortfolioCard(account="ABC:0002", statement_day=2),
    PortfolioCard(account="CCB:0004", statement_day=10),
    PortfolioCard(account="ABC:0003", statement_day=16),
    PortfolioCard(account="BOC:0005", statement_day=22),
    PortfolioCard(account="BOC:0006", statement_day=22),
]
SMTP = SmtpReportConfig(
    enabled=True, smtp_server="smtp.example.invalid",
    username="bills@example.invalid", to_addr="me@example.invalid",
)  # fmt: skip
FAKE_PDF = b"%PDF-1.7 fake statement"


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.instances.clear()


def load(conn, data_dir, folder=FIXTURES):
    for mail in DirectorySource(folder).iter_new():
        process(conn, data_dir, mail)


@pytest.fixture
def db(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    load(conn, isolated_data_dir)
    return conn, FxRates(conn, FxConfig(), FakeFrankfurter(RATES))


def report(conn, fx, cycle="2026-09", today=date(2026, 9, 19), portfolio=PORTFOLIO, new=()):
    return build_cycle_report(
        conn, cycle, fx, load_rules(), portfolio=portfolio, new_bill_ids=new, today=today
    )


def ids(conn, cycle):
    rows = conn.execute(
        "SELECT id FROM bills WHERE substr(statement_date, 1, 7) = ? ORDER BY id", (cycle,)
    )
    return [r[0] for r in rows]


def sent_messages():
    return [m for s in FakeSMTP.instances for m in s.sent]


# --- which cards, in which state -----------------------------------------------------


def test_statement_month_is_the_month_of_the_statement_date():
    assert cycle_title("2026-09") == "2026年9月"


def test_expected_cards_without_portfolio_come_from_recent_statements(db):
    conn, _ = db
    # CCB's last statement is 07-10 and BOC:0005's 08-22: both within two months of September
    assert expected_cards(conn, "2026-09") == {
        "ABC:0001": 1, "ABC:0002": 2, "ABC:0003": 16, "CCB:0004": 10, "BOC:0005": 22,
    }  # fmt: skip


def test_portfolio_leaves_out_cards_that_did_not_exist_yet(db):
    conn, _ = db
    # In June 2025 only the two BOC cards had statements; the others start in 2026.
    assert set(expected_cards(conn, "2025-06", PORTFOLIO)) == {"BOC:0005", "BOC:0006"}


def test_progress_while_cards_are_still_to_come(db):
    conn, fx = db
    r = report(conn, fx)
    states = {c.label: c.state for c in r.cards}
    assert states == {
        "农业银行 0001": "arrived", "农业银行 0002": "arrived", "建设银行 0004": "missing",
        "农业银行 0003": "arrived", "中国银行 0005": "pending", "中国银行 0006": "pending",
    }  # fmt: skip
    assert [c.label for c in r.cards][:3] == ["农业银行 0001", "农业银行 0002", "建设银行 0004"]
    assert r.progress == "已出账 3/6" and not r.complete
    assert r.status_line == "还有 2 张待出账，1 张可能无账单"
    assert r.subject == "📊 2026年9月 信用卡账单"


def test_a_card_is_pending_until_a_week_after_its_usual_day(db):
    conn, fx = db
    ccb = lambda today: next(c for c in report(conn, fx, today=today).cards if "0004" in c.label)  # noqa: E731
    assert ccb(date(2026, 9, 17)).state == "pending"  # 09-10 + 7 days
    assert ccb(date(2026, 9, 18)).state == "missing"
    assert "已过一周" in ccb(date(2026, 9, 18)).note


def test_complete_month_has_due_dates_in_order(db):
    conn, fx = db
    r = report(conn, fx, today=date(2026, 9, 30))
    assert r.complete and r.status_line == "本月账单已齐，3 张可能无账单"
    assert [(d.month, d.day, d.label) for d in r.due_lines] == [
        ("9月", 20, "农业银行 0001"), ("9月", 21, "农业银行 0002"), ("10月", 5, "农业银行 0003"),
    ]  # fmt: skip
    assert r.due_lines[-1].amount == "¥1,217.97"


def test_due_dates_are_sorted_whatever_the_card_order(db):
    conn, fx = db
    shuffled = [PortfolioCard(account=a, statement_day=d)
                for a, d in [("ABC:0003", 1), ("ABC:0002", 2), ("ABC:0001", 3)]]  # fmt: skip
    r = report(conn, fx, today=date(2026, 9, 30), portfolio=shuffled)
    assert [c.label for c in r.cards] == ["农业银行 0003", "农业银行 0002", "农业银行 0001"]
    assert [(d.month, d.day) for d in r.due_lines] == [("9月", 20), ("9月", 21), ("10月", 5)]


def test_due_total_adds_the_issued_statements_in_cny(db):
    conn, fx = db
    r = report(conn, fx)
    amounts = [D(c.amount.removeprefix("¥").replace(",", "")) for c in r.cards if c.amount]
    assert len(amounts) == 3 and r.due_total == f"{sum(amounts):,.2f}"
    assert "¥1,217.97" in [c.amount for c in r.cards]


def test_new_statements_are_marked_and_summarised(db):
    conn, fx = db
    new = ids(conn, "2026-09")[-1:]
    r = report(conn, fx, new=new)
    assert [c.label for c in r.cards if c.is_new] == [r.new_bills[0].label]
    assert len(r.new_bills) == 1


def test_zero_statement_needs_no_payment(db):
    conn, fx = db
    r = report(conn, fx, cycle="2026-08", today=date(2026, 9, 30))
    boc = next(c for c in r.cards if c.label == "中国银行 0005")
    assert boc.amount == "无需还款" and "前还款" not in boc.note


# --- the e-mail ----------------------------------------------------------------------


def email(conn, fx, cycle="2026-09", attach=lambda bill: FAKE_PDF, today=date(2026, 9, 19)):
    msg, _ = build_cycle_email(
        conn, cycle, ids(conn, cycle), fx, "a@example.invalid", "b@example.invalid",
        portfolio=PORTFOLIO, attach=attach, today=today,
    )  # fmt: skip
    return msg


def test_email_attaches_each_new_statement_as_pdf(db):
    conn, fx = db
    msg = email(conn, fx)
    files = {p.get_filename(): p.get_content() for p in msg.iter_attachments()}
    assert set(files) == {
        "AutoBill-ABC-0001-2026-09-01.pdf",
        "AutoBill-ABC-0002-2026-09-02.pdf",
        "AutoBill-ABC-0003-2026-09-16.pdf",
    }
    assert all(data.startswith(b"%PDF-") for data in files.values())
    html = msg.get_body(("html",)).get_content()
    assert "AutoBill-ABC-0003-2026-09-16.pdf</div>" in html and "完整标准账单在附件里" in html


def test_email_without_a_browser_says_so(db):
    conn, fx = db
    msg = email(conn, fx, attach=None)
    assert list(msg.iter_attachments()) == []
    assert "这次没有附完整账单 PDF" in msg.get_body(("html",)).get_content()


def test_email_is_made_for_ios_mail(db):
    conn, fx = db
    msg = email(conn, fx)
    html = msg.get_body(("html",)).get_content()
    assert 'name="format-detection" content="telephone=no, date=no' in html
    assert 'name="color-scheme" content="light dark"' in html
    assert "x-apple-data-detectors" in html and "prefers-color-scheme: dark" in html
    assert '<div class="preheader">已出账 3/6 · 合计应还 ¥' in html
    assert "<img" not in html and "href=" not in html and "http" not in html
    assert "<script" not in html and 'class="bar"' in html  # inline SVG chart
    assert "请尽快还款" not in html and "还款提醒" not in html
    assert msg["X-AutoBill-Report"] == "true"
    text = msg.get_body(("plain",)).get_content()
    assert "已出账 3/6" in text and "建设银行 0004：可能无账单" in text


def test_final_email_shows_timeline_and_all_categories(db):
    conn, fx = db
    html = email(conn, fx, today=date(2026, 9, 30)).get_body(("html",)).get_content()
    assert '<div class="sh">还款日</div>' in html and "本月合计应还" in html
    assert 'class="cal"' in html and "只列出还款日，不做提醒" in html
    unfinished = email(conn, fx).get_body(("html",)).get_content()
    assert '<div class="sh">还款日</div>' not in unfinished and "已出账合计应还" in unfinished
    assert (
        '<div class="sh">本月消费</div>' in unfinished
    )  # spending so far, before all cards are in


# --- sending: one e-mail per month per run, one conversation per month ---------------


def send(conn, fx, today, attach=None, factory=FakeSMTP):
    return send_pending_reports(
        conn, Mailer(SMTP, "secret", factory), fx,
        portfolio=PORTFOLIO, attach=attach, today=today,
    )  # fmt: skip


def test_one_email_per_month_and_never_twice(db):
    conn, fx = db
    result = send(conn, fx, date(2026, 9, 19))
    assert result.failed is None and result.emails == 5 and len(result.sent) == 8
    subjects = [m["Subject"] for m in sent_messages()]
    assert subjects == [
        "📊 2025年6月 信用卡账单", "📊 2026年6月 信用卡账单", "📊 2026年7月 信用卡账单",
        "📊 2026年8月 信用卡账单", "📊 2026年9月 信用卡账单",
    ]  # fmt: skip
    assert open_cycles(conn) == ["2026-09"]  # the only month still waiting for cards
    again = send(conn, fx, date(2026, 9, 20))
    assert again.emails == 0 and len(sent_messages()) == 5


def test_final_email_follows_when_missing_cards_run_out_of_time(db):
    conn, fx = db
    send(conn, fx, date(2026, 9, 19))
    first = sent_messages()[-1]
    result = send(conn, fx, date(2026, 9, 30))  # BOC cards now more than a week late
    assert result.emails == 1 and result.sent == []
    final = sent_messages()[-1]
    assert final["Subject"] == first["Subject"]
    assert final["In-Reply-To"] == first["Message-ID"]
    assert final["References"] == first["Message-ID"]
    assert "本月账单已齐" in final.get_body(("html",)).get_content()
    assert open_cycles(conn) == []
    assert send(conn, fx, date(2026, 10, 30)).emails == 0  # a closed month stays closed


def test_statements_arriving_together_share_one_email(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    load(conn, isolated_data_dir, FIXTURES / "abc")
    fx = FxRates(conn, FxConfig(), FakeFrankfurter(RATES))
    result = send(conn, fx, date(2026, 9, 19), attach=lambda bill: FAKE_PDF)
    assert result.emails == 1 and len(result.sent) == 3
    (msg,) = sent_messages()
    assert len(list(msg.iter_attachments())) == 3 and msg["In-Reply-To"] is None


def test_later_statement_continues_the_conversation(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    fx = FxRates(conn, FxConfig(), FakeFrankfurter(RATES))
    mails = sorted(DirectorySource(FIXTURES / "abc").iter_new(), key=lambda m: m.source)
    process(conn, isolated_data_dir, mails[0])
    send(conn, fx, date(2026, 9, 19))
    for mail in mails[1:]:
        process(conn, isolated_data_dir, mail)
    send(conn, fx, date(2026, 9, 19))
    first, second = sent_messages()
    assert second["In-Reply-To"] == first["Message-ID"]
    assert thread_ids(conn, "2026-09") == [first["Message-ID"], second["Message-ID"]]


def test_failed_send_keeps_bills_pending_and_records_nothing(db):
    conn, fx = db

    def broken(*args, **kwargs):
        return FakeSMTP(*args, **kwargs, fail_on_send=True)

    result = send(conn, fx, date(2026, 9, 19), factory=broken)
    assert result.emails == 0 and result.failed[0] == "2025-06"
    assert "SMTPServerDisconnected" in result.failed[1]
    assert len(FakeSMTP.instances) == 1  # stopped after the first failure
    pending = conn.execute("SELECT COUNT(*) FROM bills WHERE reported_at IS NULL").fetchone()[0]
    assert pending == 8 and conn.execute("SELECT COUNT(*) FROM cycle_threads").fetchone()[0] == 0


def test_thread_row_keeps_every_message_id(db):
    conn, fx = db
    send(conn, fx, date(2026, 9, 19))
    send(conn, fx, date(2026, 9, 30))
    row = conn.execute("SELECT message_ids FROM cycle_threads WHERE cycle = '2026-09'").fetchone()
    ids_ = json.loads(row[0])
    assert len(ids_) == 2 and all(re.fullmatch(r"<.+@autobill\.invalid>", i) for i in ids_)


# --- shared bar helpers --------------------------------------------------------------


def test_small_shares_never_read_zero_percent():
    rows = bar_rows([("大", D("9900")), ("小", D("30"))], total=D("9930"))
    assert [r.note for r in rows] == ["100%", "<1%"]
    assert rows[1].width == 2  # the smallest positive bar is still visible


def test_categories_fold_the_tail_and_put_uncategorised_last_in_grey():
    categories = {"未分类": D("900"), **{f"类{i}": D(100 - i) for i in range(8)}}
    rows = category_bar_rows(categories)
    assert [r.name for r in rows] == ["类0", "类1", "类2", "类3", "类4", "其他", "未分类"]
    assert rows[5].amount == "282.00"  # 类5 + 类6 + 类7 = 95 + 94 + 93
    assert rows[-1].color == COLORS["context"] and rows[0].color == ""


# --- the iOS layout: ring, stacked categories, folded transactions --------------------


def test_every_transaction_is_in_the_email_but_folded_away(db):
    """Transactions are listed (checkbox hack, closed by default), so the first screen
    shows only the summary; every line of the new statements is there."""
    conn, fx = db
    html = email(conn, fx).get_body(("html",)).get_content()
    lines = re.findall(r'data-line="(\d+)"', html)
    counts = conn.execute(
        "SELECT COUNT(*) FROM transactions t JOIN bills b ON b.id = t.bill_id"
        " WHERE substr(b.statement_date, 1, 7) = '2026-09'"
    ).fetchone()[0]
    assert len(lines) == counts == 7 + 119 + 8
    assert html.count('type="checkbox"') == 3 + 1  # one per new statement + the merchants
    assert ".panel { display: none; }" in html
    assert ".acc:checked + label + .panel { display: block; }" in html
    assert re.search(r'<input type="checkbox" id="tx1" class="acc">\s*<label for="tx1"', html)


def test_credits_read_as_plus_in_green(db):
    conn, fx = db
    html = email(conn, fx).get_body(("html",)).get_content()
    assert re.search(r'<div class="amt credit">\+?[A-Z ]*\+[0-9.,]+</div>', html)  # a rebate
    assert '<div class="amt credit">-' not in html


def test_ring_has_one_arc_per_card_in_list_order(db):
    conn, fx = db
    ring = str(report(conn, fx).ring)
    arcs = re.findall(r'class="arc (\w+)"', ring)
    assert arcs == ["arrived", "arrived", "missing", "arrived", "pending", "pending"]
    assert ">3/6<" in ring


def test_stacked_categories_name_four_and_fold_the_rest(db):
    conn, fx = db
    r = report(conn, fx)
    tones = [s.tone for s in r.segments]
    assert tones[:4] == ["s1", "s2", "s3", "s4"] and set(tones[4:]) <= {"other", "none"}
    assert r.segments[-1].name == "未分类" and r.segments[-1].tone == "none"
    # a bill's own top categories reuse the month's colours
    new = report(conn, fx, new=ids(conn, "2026-09"))
    month = {s.name: s.tone for s in new.segments}
    for bill in new.new_bills:
        assert all(g.tone == month.get(g.name, "other") for g in bill.top)


def test_merchants_across_cards(db):
    conn, fx = db
    r = report(conn, fx)
    assert 0 < len(r.merchants) <= 5
    amounts = [D(m.amount.replace(",", "")) for m in r.merchants]
    assert amounts == sorted(amounts, reverse=True)


def test_emoji_need_no_invisible_variation_selector():
    from autobill.report.style import CATEGORY_EMOJI, DEFAULT_EMOJI, TYPE_EMOJI

    for e in [*CATEGORY_EMOJI.values(), *TYPE_EMOJI.values(), DEFAULT_EMOJI]:
        assert chr(0xFE0F) not in e and chr(0x200D) not in e
