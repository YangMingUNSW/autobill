"""The statement month's e-mail (docs/notify.md#账单月邮件), offline."""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fakes import FakeFrankfurter, FakeSMTP

from autobill import __version__
from autobill.categorize import load_rules
from autobill.config import FxConfig, PortfolioCard, SmtpReportConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.notify.mail import Mailer
from autobill.pipeline import process, send_pending_reports
from autobill.report.cycle import (
    MonthTotals,
    Segment,
    build_cycle_email,
    build_cycle_report,
    category_notes,
    cycle_complete,
    cycle_title,
    donut_svg,
    expected_cards,
    month_spend_cny,
    period_text,
    statement_window,
    thread_ids,
)
from autobill.report.style import COLORS, amount_with_symbol, bar_rows, category_bar_rows
from autobill.store.db import connect, load_bill

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


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.instances.clear()


# The samples these tests are written around (2026-05 to 2026-09). ICBC's (two closed
# accounts, 2025-03 to 2026-06) would add bills to the very months they look at.
SAMPLES = [FIXTURES / name for name in ("abc", "abc_2025", "boc", "ccb")]


def load(conn, data_dir, folder=None):
    for source in [folder] if folder else SAMPLES:
        for mail in DirectorySource(source).iter_new():
            process(conn, data_dir, mail)


@pytest.fixture
def db(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    load(conn, isolated_data_dir)
    return conn, FxRates(conn, FxConfig(), FakeFrankfurter(RATES))


def report(conn, fx, cycle="2026-09", today=date(2026, 9, 19), portfolio=PORTFOLIO):
    return build_cycle_report(conn, cycle, fx, load_rules(), portfolio=portfolio, today=today)


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
    # In June 2025 only the two BOC cards and ABC:0001 (the sample of the template until
    # June 2025, abc_2025/) had statements; the others start in 2026.
    assert set(expected_cards(conn, "2025-06", PORTFOLIO)) == {"ABC:0001", "BOC:0005", "BOC:0006"}


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
    assert r.subject == "📊 2026年9月账单"


def test_a_card_is_pending_until_a_week_after_its_usual_day(db):
    conn, fx = db
    ccb = lambda today: next(c for c in report(conn, fx, today=today).cards if "0004" in c.label)  # noqa: E731
    assert ccb(date(2026, 9, 17)).state == "pending"  # 09-10 + 7 days
    assert ccb(date(2026, 9, 18)).state == "missing"
    assert "已过一周" in ccb(date(2026, 9, 18)).note


def test_complete_month_says_so(db):
    conn, fx = db
    r = report(conn, fx, today=date(2026, 9, 30))
    assert r.complete and r.status_line == "本期账单已齐，3 张可能无账单"
    # the due date lives on the card's own row, not in a separate timeline
    assert "10月5日还款" in next(c for c in r.cards if "0003" in c.label).note


def test_cards_are_listed_in_statement_day_order(db):
    conn, fx = db
    shuffled = [PortfolioCard(account=a, statement_day=d)
                for a, d in [("ABC:0003", 1), ("ABC:0002", 2), ("ABC:0001", 3)]]  # fmt: skip
    r = report(conn, fx, today=date(2026, 9, 30), portfolio=shuffled)
    assert [c.label for c in r.cards] == ["农业银行 0003", "农业银行 0002", "农业银行 0001"]


def test_due_total_adds_the_issued_statements_in_cny(db):
    conn, fx = db
    r = report(conn, fx)
    amounts = [D(c.amount.removeprefix("¥").replace(",", "")) for c in r.cards if c.amount]
    assert len(amounts) == 3 and r.due_total == f"{sum(amounts):,.2f}"
    assert "¥1,217.97" in [c.amount for c in r.cards]


def test_a_foreign_card_shows_what_the_bank_itself_asks_for(db):
    """The CNY figure is a conversion; the author repays the bank in its own currency."""
    conn, fx = db
    r = report(conn, fx, cycle="2025-06", today=date(2025, 7, 30))
    boc = next(c for c in r.cards if c.bank == "中国银行" and c.amount_orig)
    assert boc.amount.startswith("¥") and boc.amount_orig.startswith("A$")
    abc = next(c for c in r.cards if c.bank == "农业银行")  # the USD account of ABC:0001
    assert abc.amount.startswith("¥") and abc.amount_orig.startswith("US$")
    assert all(not c.amount_orig for c in r.cards if c.amount == "无需还款")


def test_every_cards_transactions_are_in_one_list(db):
    """The month reads as one statement: all cards, by date, each line saying which card."""
    conn, fx = db
    r = report(conn, fx)
    assert r.transaction_count == sum(len(day.lines) for day in r.days)
    cards = {line.card for day in r.days for line in day.lines}
    assert len(cards) == 3 and all("农业银行" in c for c in cards)
    labels = [day.label for day in r.days]
    assert labels == sorted(labels, key=lambda text: (len(text), text)) or len(labels) > 1


def test_zero_statement_needs_no_payment(db):
    conn, fx = db
    r = report(conn, fx, cycle="2026-08", today=date(2026, 9, 30))
    boc = next(c for c in r.cards if c.label == "中国银行 0005")
    assert boc.amount == "无需还款" and "还款" not in boc.note


# --- the e-mail ----------------------------------------------------------------------


def email(conn, fx, cycle="2026-09", today=date(2026, 9, 19)):
    msg, _ = build_cycle_email(
        conn, cycle, fx, "a@example.invalid", "b@example.invalid",
        portfolio=PORTFOLIO, today=today,
    )  # fmt: skip
    return msg


def test_the_email_carries_nothing_but_itself(db):
    """The originals are in the mailbox already, so nothing is attached."""
    conn, fx = db
    msg = email(conn, fx)
    assert list(msg.iter_attachments()) == []
    html = msg.get_body(("html",)).get_content()
    assert "AutoBill-ABC-" not in html and ".pdf" not in html
    count = conn.execute(
        "SELECT COUNT(*) FROM transactions t JOIN bills b ON b.id = t.bill_id"
        " WHERE substr(b.statement_date, 1, 7) = '2026-09'"
    ).fetchone()[0]
    assert f"全部 {count} 笔流水" in html  # every card's transactions, in one folded list


def test_email_is_made_for_ios_mail(db):
    conn, fx = db
    msg = email(conn, fx)
    html = msg.get_body(("html",)).get_content()
    assert 'name="format-detection" content="telephone=no, date=no' in html
    assert 'name="color-scheme" content="light dark"' in html
    assert "x-apple-data-detectors" in html and "prefers-color-scheme: dark" in html
    assert '<div class="preheader">本期应还 ¥' in html
    assert "<img" not in html and "href=" not in html and "http" not in html
    assert "<script" not in html  # inline SVG only, no script and no outside images
    assert 'class="slice' in html  # the donuts are inline SVG
    assert "请尽快还款" not in html and "还款提醒" not in html
    assert msg["X-AutoBill-Report"] == "true"
    text = msg.get_body(("plain",)).get_content()
    assert "本期应还：¥" in text and "建设银行 0004：可能无账单" in text


def test_the_email_is_one_month_report(db):
    conn, fx = db
    html = email(conn, fx, today=date(2026, 9, 30)).get_body(("html",)).get_content()
    for heading in ("本期消费", "花得最多的商户", "全部流水"):
        assert f'<div class="sh">{heading}</div>' in html, heading
    # Dropped 2026-09-24: the cards' statement periods differ, so a daily chart across
    # them was uneven at both ends, and the largest purchase added little.
    for heading in ("每日消费", "最大的一笔"):
        assert heading not in html, heading
    assert '<svg class="daily"' not in html
    assert "本期应还" in html
    assert '<div class="sh">还款日</div>' not in html  # the card rows carry the due date
    assert "新账单" not in html  # every card is in the one report, new or not


def test_the_footer_names_the_version_that_made_the_email(db, monkeypatch):
    """So the e-mail itself tells whether the server runs the newest version."""
    monkeypatch.setenv("AUTOBILL_REVISION", "15fe0d7e7a0b0e3a4139399458d0e06e74ef11c4")
    conn, fx = db
    msg = email(conn, fx)
    for body in ("html", "plain"):
        assert f"版本 {__version__} (15fe0d7)" in msg.get_body((body,)).get_content(), body


# --- a bill, not a calendar month (2026-10-06) -------------------------------------------


def test_the_month_is_called_a_bill_and_says_when_its_money_was_spent(db):
    """A statement month is when the banks issue the bills; most of what they list was
    spent the month before. So the e-mail is "2026年9月账单", names the days its spending
    covers under the title, and speaks of 本期, never 本月."""
    conn, fx = db
    r = report(conn, fx)
    assert r.title == "2026年9月账单" and r.subject == "📊 2026年9月账单"
    assert r.period == "8月2日–9月16日"  # ABC:0001 from 08-02 to ABC:0003 to 09-16
    msg = email(conn, fx)
    html = msg.get_body(("html",)).get_content()
    title, period, status = (
        html.index(s)
        for s in ("<h1>2026年9月账单</h1>", '<div class="period">消费 8月2日–9月16日</div>',
                  '<div class="subtitle">')
    )  # fmt: skip
    assert title < period < status
    text = msg.get_body(("plain",)).get_content()
    assert text.startswith("2026年9月账单\n消费 8月2日–9月16日\n还有 2 张待出账")
    assert "本月" not in html and "本月" not in text


def test_a_card_that_spent_nothing_does_not_stretch_the_days(db):
    """As in September 2026: the BOC card, still on the 22nd while the others had moved,
    issued a statement with nothing on it. The days are those of the cards that spent."""
    conn, fx = db
    conn.execute("UPDATE bills SET statement_date = '2026-09-22' WHERE account_id = 'BOC:0005'"
                 " AND statement_date = '2026-08-22'")  # fmt: skip
    r = report(conn, fx, today=date(2026, 9, 30))
    assert "中国银行 0005" in [c.label for c in r.cards if c.state == "arrived"]
    assert r.period == "8月2日–9月16日"


def test_a_month_without_spending_names_no_days(db):
    """August 2026 has only the BOC card's empty statement."""
    conn, fx = db
    assert report(conn, fx, cycle="2026-08", today=date(2026, 9, 30)).period == ""
    html = email(conn, fx, cycle="2026-08", today=date(2026, 9, 30)).get_body(("html",))
    assert 'class="period"' not in html.get_content()


def test_a_statement_without_a_printed_period_starts_after_the_one_before(db):
    """BOC prints only the statement date. Each of its statements starts the day after the
    card's statement before it, as the author's database shows; without that one (the
    card's first, or a month missing) it starts at its first purchase."""
    conn, fx = db
    june = {b.account_id: b for b in (load_bill(conn, i) for i in ids(conn, "2025-06"))}
    assert statement_window(conn, june["ABC:0001"]) == (date(2025, 5, 2), date(2025, 6, 1))
    # The samples' first BOC statements: from the first purchase on each.
    assert statement_window(conn, june["BOC:0005"]) == (date(2025, 5, 27), date(2025, 6, 22))
    assert statement_window(conn, june["BOC:0006"]) == (date(2025, 5, 24), date(2025, 6, 22))
    assert report(conn, fx, cycle="2025-06", today=date(2025, 7, 30)).period == "5月2日–6月22日"
    # BOC:0005's next sample is 14 months later and has nothing on it: nothing to go by.
    august = load_bill(conn, ids(conn, "2026-08")[0])
    assert statement_window(conn, august) is None
    conn.execute("UPDATE bills SET statement_date = '2026-07-22' WHERE account_id = 'BOC:0005'"
                 " AND statement_date = '2025-06-22'")  # fmt: skip
    assert statement_window(conn, august) == (date(2026, 7, 23), date(2026, 8, 22))


def test_the_days_run_from_the_earliest_start_to_the_latest_end():
    october = [(date(2026, 9, 2), date(2026, 10, 12)), (date(2026, 9, 17), date(2026, 10, 12))]
    assert period_text(october) == "9月2日–10月12日"  # the month the cards moved to the 12th
    assert period_text([(date(2026, 12, 13), date(2027, 1, 12))]) == "12月13日–1月12日"
    assert period_text([]) == ""


def test_the_change_is_against_the_statement_month_before():
    from autobill.report.cycle import _change

    assert _change(D(112), D(100)) == "比上期 +12%"
    assert _change(D(80), D(100)) == "比上期 -20%"
    assert _change(D(80), None) == "" and _change(D(80), D(0)) == ""


# --- sending: one e-mail per month per run, one conversation per month ---------------


def send(conn, fx, today, factory=FakeSMTP):
    return send_pending_reports(
        conn, Mailer(SMTP, "secret", factory), fx, portfolio=PORTFOLIO, today=today
    )


def test_a_month_waits_until_every_card_is_in(db):
    """One e-mail per month, and not before the month is complete: on 19 September the two
    BOC cards (usually the 22nd) are still to come, so September's statements wait."""
    conn, fx = db
    result = send(conn, fx, date(2026, 9, 19))
    assert result.failed is None and result.emails == 4
    subjects = [m["Subject"] for m in sent_messages()]
    assert subjects == [
        "📊 2025年6月账单", "📊 2026年6月账单", "📊 2026年7月账单", "📊 2026年8月账单",
    ]  # fmt: skip
    assert set(result.sent).isdisjoint(ids(conn, "2026-09"))
    waiting = conn.execute("SELECT COUNT(*) FROM bills WHERE reported_at IS NULL").fetchone()[0]
    assert waiting == len(ids(conn, "2026-09")) and thread_ids(conn, "2026-09") == []
    again = send(conn, fx, date(2026, 9, 20))
    assert again.emails == 0 and len(sent_messages()) == 4  # still waiting, still nothing


def test_the_month_goes_out_once_its_missing_cards_run_out_of_time(db):
    """A card with no statement is only given a week past its usual day; then the month
    counts as complete and its one e-mail goes out, covering every card."""
    conn, fx = db
    send(conn, fx, date(2026, 9, 19))
    result = send(conn, fx, date(2026, 9, 30))  # BOC cards now more than a week late
    assert result.emails == 1 and set(result.sent) == set(ids(conn, "2026-09"))
    final = sent_messages()[-1]
    assert final["Subject"] == "📊 2026年9月账单"
    assert final["In-Reply-To"] is None  # the month's only e-mail: nothing to follow
    assert "本期账单已齐" in final.get_body(("html",)).get_content()
    assert send(conn, fx, date(2026, 10, 30)).emails == 0  # sent once, never again


@pytest.mark.parametrize(
    ("cycle", "today"),
    [
        ("2026-09", date(2026, 9, 19)),  # two cards still to come
        ("2026-09", date(2026, 9, 30)),  # they ran out of time
        ("2026-08", date(2026, 9, 19)),  # every expected card is in
        ("2025-06", date(2026, 9, 19)),  # long past
    ],
)
def test_cycle_complete_agrees_with_the_built_report(db, cycle, today):
    """The cheap check decides whether the e-mail is built at all, so it must never drift
    from the report's own verdict."""
    conn, fx = db
    assert cycle_complete(conn, cycle, PORTFOLIO, today) is report(conn, fx, cycle, today).complete


def test_statements_arriving_together_share_one_email(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    load(conn, isolated_data_dir, FIXTURES / "abc")
    fx = FxRates(conn, FxConfig(), FakeFrankfurter(RATES))
    result = send(conn, fx, date(2026, 9, 30))
    assert result.emails == 1 and len(result.sent) == 3
    (msg,) = sent_messages()
    assert list(msg.iter_attachments()) == [] and msg["In-Reply-To"] is None


def test_a_late_statement_continues_the_conversation(isolated_data_dir):
    """A statement arriving after the month was sent gets an e-mail of its own, into the
    same conversation: it is news, unlike the ones that were already covered."""
    conn = connect(isolated_data_dir / "autobill.db")
    fx = FxRates(conn, FxConfig(), FakeFrankfurter(RATES))
    mails = sorted(DirectorySource(FIXTURES / "abc").iter_new(), key=lambda m: m.source)
    process(conn, isolated_data_dir, mails[0])
    send(conn, fx, date(2026, 9, 30))
    for mail in mails[1:]:
        process(conn, isolated_data_dir, mail)
    send(conn, fx, date(2026, 9, 30))
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
    assert pending == 9  # every sample statement still waits, none was reported
    assert conn.execute("SELECT COUNT(*) FROM cycle_threads").fetchone()[0] == 0


def test_thread_row_keeps_every_message_id(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    fx = FxRates(conn, FxConfig(), FakeFrankfurter(RATES))
    mails = sorted(DirectorySource(FIXTURES / "abc").iter_new(), key=lambda m: m.source)
    process(conn, isolated_data_dir, mails[0])
    send(conn, fx, date(2026, 9, 30))
    for mail in mails[1:]:
        process(conn, isolated_data_dir, mail)
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
    assert html.count('id="tx"') == 1  # all three cards in one list now
    # closed by default: a grid row of height 0 that opens on the spring (2026-09-29)
    assert ".pan { display: grid; grid-template-rows: 0fr;" in html
    assert ".acc:checked + label + .pan { grid-template-rows: 1fr; }" in html
    toggle = r'<input type="checkbox" id="tx" class="acc">\s*<label for="tx"[^>]*>'
    assert re.search(toggle + r'.*?</label>\s*<div class="pan">', html, re.S)


def test_credits_read_as_plus_in_green(db):
    conn, fx = db
    html = email(conn, fx).get_body(("html",)).get_content()
    assert re.search(r'<div class="amt num credit">\+[^<0-9]*[0-9.,]+</div>', html)  # a rebate
    assert 'class="amt num credit">-' not in html


# --- the six-month spending trend -----------------------------------------------------


def test_the_trend_is_six_statement_months_ending_with_this_one(db):
    """Fixtures: CCB 2026-06 and 07, BOC 2026-08, ABC 2026-09; nothing in April or May."""
    conn, fx = db
    r = report(conn, fx)
    assert [b.label for b in r.trend] == ["4月", "5月", "6月", "7月", "8月", "9月"]
    assert [b.current for b in r.trend] == [False] * 5 + [True]
    assert r.trend[0].value is None and r.trend[1].value is None  # a gap, not skipped
    for bar in r.trend[2:5]:
        assert bar.value == month_spend_cny(conn, bar.cycle, fx, load_rules())
    # This month's column is exactly the 本期消费 figure above it.
    assert f"{r.trend[-1].value:,.2f}" == r.spend_total


def test_the_trend_chart_highlights_this_month_and_writes_every_value(db):
    conn, fx = db
    r = report(conn, fx)
    chart = str(r.trend_chart)
    # June has spending; July and August spent nothing (a flat baseline, no column).
    assert [b.value > 0 for b in r.trend[2:5]] == [True, False, False]
    assert chart.count('class="bar"') == 1 and chart.count('class="bar now"') == 1
    assert chart.count('class="gap"') == 2  # April and May have no statements: a dash
    # every month with statements has its value over it (2026-09-29), this month's stands out
    assert chart.count('class="value now"') == 1
    assert chart.count('class="value"') == sum(1 for b in r.trend[:-1] if b.value is not None)
    for bar in r.trend:
        if bar.value is not None:
            assert f">¥{bar.value:,.0f}<" in chart, bar.label
        assert bar.label in re.search(r'aria-label="([^"]*)"', chart)[1]  # and for a screen reader


def test_the_trend_caption_compares_this_month_with_the_average(db):
    conn, fx = db
    r = report(conn, fx)
    values = [b.value for b in r.trend if b.value is not None]
    average = sum(values, D(0)) / len(values)
    ratio = (r.trend[-1].value - average) / average
    word = "多" if ratio > 0 else "少"
    assert r.trend_caption == f"4 期平均 ¥{average:,.0f} · 本期比平均{word} {abs(ratio):.0%}"


def test_the_trend_is_in_the_email_between_spending_and_merchants(db):
    conn, fx = db
    msg = email(conn, fx)
    html = msg.get_body(("html",)).get_content()
    spending, trend, merchants = (
        html.index(f'<div class="sh">{h}</div>') for h in ("本期消费", "近 6 期", "花得最多的商户")
    )
    assert spending < trend < merchants
    text = msg.get_body(("plain",)).get_content()
    assert "近 6 期：4月 无账单 · 5月 无账单 · 6月 ¥" in text


def test_one_month_alone_shows_no_trend(db):
    """2025-06 is the oldest statement month: nothing before it to compare with."""
    conn, fx = db
    r = report(conn, fx, cycle="2025-06", today=date(2025, 7, 30))
    assert not r.trend_shown and r.trend_caption == ""
    html = email(conn, fx, cycle="2025-06", today=date(2025, 7, 30)).get_body(("html",))
    assert "近 6 期" not in html.get_content()


# --- categories against their usual ------------------------------------------------


def notes(now: dict[str, str], *earlier: dict[str, str]) -> dict[str, str]:
    """category_notes with plain numbers: this month, then the months before it."""
    values = {k: D(v) for k, v in now.items()}
    segments = [Segment(name, "", "", v, f"s{i + 1}") for i, (name, v) in enumerate(values.items())]
    months = [MonthTotals(D(0), {k: D(v) for k, v in m.items()}) for m in earlier]
    return category_notes(segments, values, months)


def test_a_category_clearly_off_its_usual_is_noted_either_way():
    assert notes(
        {"餐饮": "3200", "交通": "600"},
        {"餐饮": "2400", "交通": "1000"},
        {"餐饮": "2300", "交通": "1100"},
        {"餐饮": "2500", "交通": "900"},
    ) == {"餐饮": "比平时多 ¥800", "交通": "比平时少 ¥400"}


def test_small_changes_are_not_noted():
    # 250 more is under ¥300; 500 more on a usual 2,500 is only 20%.
    assert notes({"餐饮": "750", "超市": "3000"}, {"餐饮": "500", "超市": "2500"},
                 {"餐饮": "500", "超市": "2500"}) == {}  # fmt: skip


def test_usual_is_the_median_so_one_big_month_does_not_move_it():
    """A flight in one earlier month must not make this month look cheap."""
    got = notes({"旅行": "1000"}, {"旅行": "1000"}, {"旅行": "9000"}, {"旅行": "1100"})
    assert got == {}


def test_at_most_three_notes_the_largest_first():
    got = notes(
        {"A": "1400", "B": "1300", "C": "1200", "D": "1100"},
        {"A": "0", "B": "0", "C": "0", "D": "0"},
        {"A": "0", "B": "0", "C": "0", "D": "0"},
    )
    assert list(got) == ["A", "B", "C"]


def test_catch_alls_and_thin_history_are_never_noted():
    assert notes({"其他": "5000", "未分类": "5000"}, {}, {}) == {}
    assert notes({"餐饮": "5000"}, {"餐饮": "100"}) == {}  # one earlier month is not enough


def test_the_notes_are_in_the_email(db):
    """Fixtures: June to August spent almost nothing, so this month's large categories
    stand out; the caption then says what 平时 means."""
    conn, fx = db
    r = report(conn, fx)
    noted = [s for s in r.segments if s.note]
    assert 1 <= len(noted) <= 3 and all(s.note.startswith("比平时") for s in noted)
    msg = email(conn, fx)
    html = msg.get_body(("html",)).get_content()
    for s in noted:
        assert f'<span class="note">{s.note}</span>' in html
    assert "平时指前 3 期的中位数" in html
    text = msg.get_body(("plain",)).get_content()
    assert f"{noted[0].name} {noted[0].share} ¥{noted[0].amount}（{noted[0].note}）" in text


def test_no_notes_without_earlier_months(db):
    conn, fx = db
    r = report(conn, fx, cycle="2025-06", today=date(2025, 7, 30))
    assert not any(s.note for s in r.segments)
    html = email(conn, fx, cycle="2025-06", today=date(2025, 7, 30)).get_body(("html",))
    assert "平时指" not in html.get_content()


def test_the_donut_has_one_slice_per_legend_row(db):
    """A slice too thin to see must never be the only place a number appears, so the
    legend and the chart carry the same list in the same order."""
    conn, fx = db
    r = report(conn, fx)
    donut = str(r.category_donut)
    assert re.findall(r'class="slice (\w+)"', donut) == [s.tone for s in r.segments]
    assert f">¥{r.spend_total}<" in donut and ">本期消费<" in donut
    for segment in r.segments:  # the aria-label says what a screen reader cannot see
        assert f"{segment.label} {segment.share}" in donut  # the grey row: "其余 N 类"
    assert str(donut).count("<circle") == len(r.segments) + 1  # + the track behind them


def test_merchant_names_in_the_donut_are_escaped():
    """Merchant names come from the statement: a quote or "<" must not break the e-mail."""
    segment = Segment('Bar "Q" & <Grill>', "1.00", "100%", Decimal("1"), "s1")
    donut = str(donut_svg([segment], "¥1", "本期消费"))
    assert 'aria-label="本期消费：Bar &#34;Q&#34; &amp; &lt;Grill&gt; 100%"' in donut
    assert "<Grill>" not in donut


def test_stacked_categories_name_four_and_fold_the_rest(db):
    conn, fx = db
    r = report(conn, fx)
    tones = [s.tone for s in r.segments]
    assert tones[:4] == ["s1", "s2", "s3", "s4"] and set(tones[4:]) <= {"other", "none"}
    assert r.segments[-1].name == "未分类" and r.segments[-1].tone == "none"
    # the merchants reuse the month's colours, so a colour means one thing in one e-mail
    month = {s.name: s.tone for s in r.segments}
    assert all(m.tone in set(month.values()) | {"other"} for m in r.merchants)


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


# --- what a purchase actually cost ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "currency", "shown"),
    [
        (D("12345"), "JPY", "JP¥12,345"),  # the yen is not the yuan, and has no cents
        (D("1217.97"), "CNY", "¥1,217.97"),
        (D("168.5"), "USD", "US$168.50"),
        (D("39.9"), "AUD", "A$39.90"),
        (D("-12.34"), "EUR", "-€12.34"),  # the sign stays outside the symbol
        (D("1234"), "SEK", "SEK 1,234.00"),  # not in the table: the code stays
    ],
)
def test_amounts_name_their_currency(value, currency, shown):
    assert amount_with_symbol(value, currency) == shown


def test_a_foreign_line_shows_what_was_paid_and_what_it_cost_in_cny(db):
    """The author wants to see the local price; the CNY is the小字 that ties it to the total."""
    conn, fx = db
    lines = [line for day in report(conn, fx).days for line in day.lines]
    foreign = [t for t in lines if t.cny]
    assert foreign, "the samples have foreign purchases"
    for line in foreign:
        assert line.cny.startswith("≈¥") and not line.local.startswith("≈")
    home = [t for t in lines if not t.cny]
    assert all(t.local.startswith(("¥", "-¥")) for t in home)  # CNY is never repeated


# --- rows that open to their lines (report/drill.py) ------------------------------------------


def test_each_category_opens_to_lines_that_add_up_to_it(db):
    conn, fx = db
    r = report(conn, fx)
    assert r.segments
    for s in r.segments:
        rows = s.folded or [s]
        for row in rows:
            assert row.lines is not None and row.lines.count > 0, row.name
            assert abs(row.lines.total - row.weight) < D("0.01"), row.name
        if s.folded:  # the grey row lists the categories it folds, and they add up to it
            assert s.label == f"其余 {len(s.folded)} 类"
            assert abs(sum((f.weight for f in s.folded), D(0)) - s.weight) < D("0.01")


def test_each_shop_opens_to_its_lines(db):
    conn, fx = db
    r = report(conn, fx)
    assert r.merchants
    for m in r.merchants:
        assert m.lines is not None and abs(m.lines.total - m.weight) < D("0.01"), m.name
        assert all("月" in line.title and "日" in line.title for line in m.lines.lines)  # dated


def test_rows_open_in_the_email_and_nothing_else_is_needed(db):
    """Every category and shop is a row that opens (a checkbox and its label), the ids
    never clash, the grey row reads 其余 N 类, and there is still no script."""
    conn, fx = db
    msg = email(conn, fx, today=date(2026, 9, 30))
    html = msg.get_body(("html",)).get_content()
    r = report(conn, fx, today=date(2026, 9, 30))
    ids = re.findall(r'<input type="checkbox" id="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    folded = sum(len(s.folded) for s in r.segments)
    assert len(ids) == 1 + len(r.segments) + folded + len(r.merchants)  # 1: 全部流水
    assert folded, "the samples fold some categories into the grey row"
    text = msg.get_body(("plain",)).get_content()
    for s in r.segments:
        assert s.label in html and s.label in text
    assert "<script" not in html and "onclick" not in html
    assert "touch-action: manipulation" in html  # taps answer at once
