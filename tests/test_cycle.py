"""The statement month's e-mail (docs/notify.md#账单月邮件), offline."""

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
    Segment,
    build_cycle_email,
    build_cycle_report,
    cycle_complete,
    cycle_title,
    donut_svg,
    expected_cards,
    thread_ids,
)
from autobill.report.style import COLORS, amount_with_symbol, bar_rows, category_bar_rows
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


def test_complete_month_says_so(db):
    conn, fx = db
    r = report(conn, fx, today=date(2026, 9, 30))
    assert r.complete and r.status_line == "本月账单已齐，3 张可能无账单"
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
    boc = next(c for c in r.cards if c.amount_orig)
    assert boc.amount.startswith("¥") and boc.amount_orig.startswith("A$")
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


def test_the_largest_purchase_is_picked_out(db):
    conn, fx = db
    r = report(conn, fx)
    spent = [
        line
        for day in r.days
        for line in day.lines
        if not line.excluded and not line.tag and line.cny_value is not None
    ]
    assert r.biggest is not None
    assert r.biggest.local == max(spent, key=lambda t: t.cny_value).local


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
    assert '<div class="preheader">本月合计应还 ¥' in html
    assert "<img" not in html and "href=" not in html and "http" not in html
    assert "<script" not in html  # inline SVG only, no script and no outside images
    assert 'class="bar"' in html and 'class="slice' in html  # daily chart and donut
    assert "请尽快还款" not in html and "还款提醒" not in html
    assert msg["X-AutoBill-Report"] == "true"
    text = msg.get_body(("plain",)).get_content()
    assert "本月合计应还：¥" in text and "建设银行 0004：可能无账单" in text


def test_the_email_is_one_month_report(db):
    conn, fx = db
    html = email(conn, fx, today=date(2026, 9, 30)).get_body(("html",)).get_content()
    for heading in ("本月消费", "每日消费", "最大的一笔", "花得最多的商户", "全部流水"):
        assert f'<div class="sh">{heading}</div>' in html, heading
    assert "本月合计应还" in html
    assert '<div class="sh">还款日</div>' not in html  # the card rows carry the due date
    assert "新账单" not in html  # every card is in the one report, new or not


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
        "📊 2025年6月 信用卡账单", "📊 2026年6月 信用卡账单", "📊 2026年7月 信用卡账单",
        "📊 2026年8月 信用卡账单",
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
    assert final["Subject"] == "📊 2026年9月 信用卡账单"
    assert final["In-Reply-To"] is None  # the month's only e-mail: nothing to follow
    assert "本月账单已齐" in final.get_body(("html",)).get_content()
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
    assert pending == 8 and conn.execute("SELECT COUNT(*) FROM cycle_threads").fetchone()[0] == 0


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
    assert html.count('type="checkbox"') == 1  # all three cards in one list now
    assert ".panel { display: none; }" in html
    assert ".acc:checked + label + .panel { display: block; }" in html
    assert re.search(r'<input type="checkbox" id="tx" class="acc">\s*<label for="tx"', html)


def test_credits_read_as_plus_in_green(db):
    conn, fx = db
    html = email(conn, fx).get_body(("html",)).get_content()
    assert re.search(r'<div class="amt num credit">\+[^<0-9]*[0-9.,]+</div>', html)  # a rebate
    assert 'class="amt num credit">-' not in html


def test_the_donut_has_one_slice_per_legend_row(db):
    """A slice too thin to see must never be the only place a number appears, so the
    legend and the chart carry the same list in the same order."""
    conn, fx = db
    r = report(conn, fx)
    donut = str(r.category_donut)
    assert re.findall(r'class="slice (\w+)"', donut) == [s.tone for s in r.segments]
    assert f">¥{r.spend_total}<" in donut and ">本月消费<" in donut
    for segment in r.segments:  # the aria-label says what a screen reader cannot see
        assert f"{segment.name} {segment.share}" in donut
    assert str(donut).count("<circle") == len(r.segments) + 1  # + the track behind them


def test_merchant_names_in_the_donut_are_escaped():
    """Merchant names come from the statement: a quote or "<" must not break the e-mail."""
    segment = Segment('Bar "Q" & <Grill>', "1.00", "100%", Decimal("1"), "s1")
    donut = str(donut_svg([segment], "¥1", "本月消费"))
    assert 'aria-label="本月消费：Bar &#34;Q&#34; &amp; &lt;Grill&gt; 100%"' in donut
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
