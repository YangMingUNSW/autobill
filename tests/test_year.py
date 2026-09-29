"""The year in review (docs/notify.md#年度回顾), offline."""

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fakes import FakeFrankfurter, FakeSMTP
from typer.testing import CliRunner

from autobill import __version__
from autobill import fx as fx_module
from autobill.categorize import load_rules
from autobill.cli import _send_reports, app
from autobill.config import FxConfig, SmtpReportConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.model import Bill, BillBalance, Transaction, TxnType, make_txn_id
from autobill.notify.mail import Mailer
from autobill.pipeline import process, send_year_review
from autobill.report.cycle import MonthBar, Segment, record_sent, trend_svg
from autobill.report.monthly import monthly_summary
from autobill.report.year import (
    _month_list,
    build_year_report,
    due_year,
    render_year_html,
    year_plain_text,
)
from autobill.store.db import connect, save_bill

FIXTURES = Path(__file__).parent / "fixtures"
D = Decimal
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109",
         ("AUD", "2026-08-24"): "4.6200", ("AUD", "2025-06-25"): "4.7000"}  # fmt: skip
SMTP = SmtpReportConfig(
    enabled=True, smtp_server="smtp.example.invalid",
    username="bills@example.invalid", to_addr="me@example.invalid",
)  # fmt: skip
runner = CliRunner()


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.instances.clear()


def sent_messages():
    return [m for s in FakeSMTP.instances for m in s.sent]


def load_samples(conn, data_dir):
    for mail in DirectorySource(FIXTURES).iter_new():
        process(conn, data_dir, mail)


@pytest.fixture
def samples(isolated_data_dir):
    """The anonymised sample statements: their 2026 transactions run from May to September."""
    conn = connect(isolated_data_dir / "autobill.db")
    load_samples(conn, isolated_data_dir)
    return conn, FxRates(conn, FxConfig(), FakeFrankfurter(RATES)), load_rules(conn)


def year_of(samples, year=2026):
    conn, fx, rules = samples
    return build_year_report(conn, year, fx, rules)


def txn(line_no, day, description, amount, currency="CNY", txn_type=TxnType.PURCHASE, **extra):
    return Transaction(
        line_no=line_no,
        txn_id=make_txn_id("ABC", "ABC:0001", day, D(amount), description, line_no),
        trans_date=day,
        txn_type=txn_type,
        amount=D(amount),
        currency=currency,
        description_raw=description,
        **extra,
    )


def bill(statement, txns, currency="CNY"):
    charges = sum((t.amount for t in txns if t.amount > 0), D(0))
    return Bill(
        bank="ABC",
        account_id="ABC:0001",
        cards=["0001"],
        statement_date=statement,
        email_date=statement + timedelta(days=1),
        balances=[
            BillBalance(
                currency=currency,
                previous_balance=D(0),
                new_charges=charges,
                payments_credits=D(0),
                amount_due=charges,
            )
        ],
        transactions=txns,
        status="OK",
        source_message_id=f"<{statement}@example.invalid>",
        source_sha256="0" * 64,
        parser_name="test",
        parser_version=1,
    )


def year_from(conn, year, rates=None):
    fx = FxRates(conn, FxConfig(), FakeFrankfurter(rates or {}))
    return build_year_report(conn, year, fx, load_rules(conn))


# --- the numbers: a calendar year by transaction date --------------------------------


def test_the_months_are_calendar_months_as_report_month_counts_them(samples):
    """Every transaction to the month of its own date, as `autobill report --month` does
    (it rounds each statement's line to cents, hence the small tolerance)."""
    conn, fx, rules = samples
    report = year_of(samples)
    for bar in report.bars:
        cny = [x.cny for x in monthly_summary(conn, bar.cycle, fx, rules).lines if x.cny]
        if not cny:
            assert bar.value is None, bar.cycle
        else:
            assert abs(bar.value - sum(cny, D(0))) <= D("0.01") * len(cny), bar.cycle
    assert [b.label for b in report.bars] == [f"{m}月" for m in range(1, 13)]
    assert report.spend == sum((b.value for b in report.bars if b.value is not None), D(0))
    assert report.months >= 2


def test_the_categories_are_the_calendar_months_categories(samples):
    conn, fx, rules = samples
    report = year_of(samples)
    summed: dict[str, Decimal] = {}
    for bar in report.bars:
        for name, value in monthly_summary(conn, bar.cycle, fx, rules).categories.items():
            summed[name] = summed.get(name, D(0)) + value
    rows = {s.name: s.weight for s in report.categories}
    assert set(rows) == {k for k, v in summed.items() if v > 0}
    for name, value in rows.items():  # the terminal report rounds each month to cents
        assert abs(value - summed[name]) <= D("0.12"), name
    tones = [s.tone for s in report.categories if s.tone != "none"]
    assert tones[:4] == ["s1", "s2", "s3", "s4"][: len(tones)]
    assert set(tones[4:]) <= {"other"}  # beyond the donut's four colours, grey


def test_a_statement_across_new_year_is_split_between_the_years(isolated_data_dir):
    """The statement issued on 12 January covers 12 December to 12 January: December's
    part belongs to the year before."""
    conn = connect(isolated_data_dir / "autobill.db")
    save_bill(conn, bill(date(2026, 1, 12), [
        txn(1, date(2025, 12, 28), "SHOP", "7.00"),
        txn(2, date(2026, 1, 3), "SHOP", "10.00"),
    ]), None)  # fmt: skip
    save_bill(conn, bill(date(2027, 1, 12), [
        txn(1, date(2026, 12, 20), "SHOP", "100.00"),
        txn(2, date(2027, 1, 5), "SHOP", "40.00"),
    ]), None)  # fmt: skip
    report = year_from(conn, 2026)
    assert (report.bars[0].value, report.bars[11].value) == (D("10.00"), D("100.00"))
    assert report.spend == D("110.00") and report.complete
    assert year_from(conn, 2025).spend == D("7.00")
    assert year_from(conn, 2027).spend == D("40.00")


def test_the_biggest_month_is_the_one_in_the_accent(samples):
    report = year_of(samples)
    current = [b for b in report.bars if b.current]
    assert len(current) == 1
    assert current[0].value == max(b.value for b in report.bars if b.value is not None)
    assert report.trend_caption.startswith(f"最多：{current[0].label} ¥")
    assert not report.complete  # no January 2027 statements yet
    assert report.span.endswith(f"截至 {report.through.month}月{report.through.day}日")


def test_charges_are_the_interest_and_fee_categories(samples):
    report = year_of(samples)
    rows = {s.name: s.weight for s in report.categories}
    assert report.charges == rows.get("利息", D(0)) + rows.get("手续费", D(0))
    assert report.rebates >= 0


def test_charges_count_a_fee_the_bank_typed_as_a_purchase(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    june = [
        txn(1, date(2026, 5, 20), "增值服务费-安心用年版", "30.00"),  # a purchase to the bank
        txn(2, date(2026, 5, 21), "利息", "5.00", txn_type=TxnType.INTEREST),
        txn(3, date(2026, 5, 22), "SHOP", "100.00"),
    ]
    save_bill(conn, bill(date(2026, 6, 1), june), None)
    assert year_from(conn, 2026).charges == D("35.00")  # by type alone it would be 5.00


def test_a_year_is_compared_only_with_a_complete_year_before(samples):
    report = year_of(samples)
    assert report.previous is None and report.change == ""  # 2025 has a few months only
    report.previous, report.spend = D("100"), D("112")
    assert report.change == "比 2025 年 +12%"
    report.spend = D("80")
    assert report.change == "比 2025 年 -20%"


def test_one_shop_under_several_names_is_one_row(isolated_data_dir):
    """The bank glues its payment markers to the name; over a year one shop would show up
    two or three times."""
    conn = connect(isolated_data_dir / "autobill.db")
    march = [
        txn(1, date(2026, 2, 10), "Woolworths Online", "100.00"),
        txn(2, date(2026, 2, 12), "Woolworths OnlineAUSVISA Apple Pay", "50.00"),
    ]
    april = [
        txn(1, date(2026, 3, 5), "跨行消费 Woolworths Online", "30.00"),
        txn(2, date(2026, 3, 9), "SOMEWHERE ELSE", "20.00"),
    ]
    save_bill(conn, bill(date(2026, 3, 1), march), None)
    save_bill(conn, bill(date(2026, 4, 1), april), None)
    report = year_from(conn, 2026)
    assert (report.merchants[0].name, report.merchants[0].amount) == ("Woolworths Online", "180.00")
    assert [(v.name, v.count) for v in report.visits] == [("Woolworths Online", 3)]  # once: no


def test_purchases_count_in_the_currency_they_were_paid_in(isolated_data_dir):
    """A purchase in Sydney on a USD card is Australian dollars; the amount in that
    currency is written under the name."""
    conn = connect(isolated_data_dir / "autobill.db")
    may = [
        txn(1, date(2026, 4, 20), "SHOP IN SYDNEY", "10.00", "USD",
            orig_amount=D("15.00"), orig_currency="AUD"),
        txn(2, date(2026, 4, 21), "CASHBACK", "-1.00", "USD", txn_type=TxnType.REBATE),
    ]  # fmt: skip
    save_bill(conn, bill(date(2026, 5, 1), may, currency="USD"), None)
    save_bill(conn, bill(date(2026, 6, 1), [txn(1, date(2026, 5, 20), "SHOP", "30.00")]), None)
    report = year_from(conn, 2026, {("USD", "2026-05-01"): "7.0"})
    assert [(c.name, c.amount, c.note) for c in report.currencies] == [
        ("澳元", "70.00", "A$15.00"),
        ("人民币", "30.00", ""),
    ]
    assert report.rebates == D("7.0")  # positive, and already taken off the spending
    assert report.spend == D("93.0")


def segments(n):
    return [Segment(f"类{i}", "1.00", "1%", D(n - i), "other") for i in range(n)]


def test_the_long_tail_of_categories_folds_away(samples):
    """A month's list (0: the year) shows the SHOWN_CATEGORIES largest and folds the rest
    under 其余 N 类, unless that would fold away a single row."""
    values = [(j, f"类{j}", D(13 - j)) for j in range(1, 13)]  # twelve categories, 12 to 1
    nine = _month_list(0, values[:9], {}, D(100))
    assert len(nine.shown) == 9 and nine.folded == []
    twelve = _month_list(0, values, {}, D(100))
    assert len(twelve.shown) == 8
    assert [c.segment.name for c in twelve.folded] == ["类9", "类10", "类11", "类12"]
    assert (twelve.folded_amount, twelve.folded_share) == ("10.00", "13%")  # 4+3+2+1 of 78
    report = year_of(samples)
    report.explorer.lists[0] = twelve
    html = render_year_html(report)
    assert "其余 4 类" in html
    assert re.search(r'<input type="checkbox" id="more-categories-0" class="acc">\s*<label', html)


# --- the filter by month and category ------------------------------------------------


def test_every_choice_adds_up(samples):
    """The headline of each choice: a whole month (spending, refunds taken off) is its bar;
    a category's twelve months add up to its year; a month's categories to its list."""
    report = year_of(samples)
    x = report.explorer
    money_of = lambda text: D(text.replace(",", ""))  # noqa: E731
    assert x.heads[(0, 0)].amount == report.spend_total
    for bar in report.bars:
        month = int(bar.label.rstrip("月"))
        want = bar.value if bar.value is not None else D(0)
        assert abs(money_of(x.heads[(month, 0)].amount) - want) < D("0.01"), bar.label
    for j in range(1, len(x.categories) + 1):
        year = money_of(x.heads[(0, j)].amount)
        months = sum((money_of(x.heads[(i, j)].amount) for i in range(1, 13)), D(0))
        assert abs(year - months) < D("0.05"), x.categories[j - 1]
    for i in range(13):
        rows = x.lists[i].shown + x.lists[i].folded
        listed = sum((c.segment.weight for c in rows), D(0))
        chosen = sum(
            (money_of(x.heads[(i, j)].amount) for j in range(1, len(x.categories) + 1)), D(0)
        )
        assert abs(listed - chosen) < D("0.05"), i
    assert len(x.lines) == sum(
        int(x.heads[(0, j)].note.split(" ")[0]) for j in range(1, len(x.categories) + 1)
    )


def test_the_filter_is_one_rule_per_value(samples):
    """Never a rule per combination: with K categories there are 13 + K + 1 blocks to show,
    one height per category, and one filter of the lines per month and per category."""
    x = year_of(samples).explorer
    k = len(x.categories)
    css = str(x.css)
    assert css.count(".x .xm.m") == 13 and css.count(".x .xc.c") == k + 1
    assert css.count(".x .bars .f { --h: var(--h") == k + 1
    assert css.count(".x .t:not(.m") == 12 and css.count(".x .t:not(.c") == k
    for bar in x.bars:  # every bar knows its height and label for every category
        assert bar.style.count("--h") == k + 1 and bar.style.count("--t") == k + 1


def test_the_email_has_the_filter_and_no_script(samples):
    report = year_of(samples)
    html = render_year_html(report)
    k = len(report.explorer.categories)
    radios = re.findall(
        r'<input type="radio" name="(x[mc])" id="([mc]\d+)" class="rd"( checked)?>', html
    )
    assert [r[1] for r in radios] == [f"m{i}" for i in range(13)] + [f"c{j}" for j in range(k + 1)]
    assert [r[1] for r in radios if r[2]] == ["m0", "c0"]  # the whole year, every category
    boxes = re.findall(r'<input type="checkbox" id="([^"]+)"', html)
    assert len(boxes) == len(set(boxes))
    assert "<script" not in html and "onclick" not in html
    assert "@property --h" in html and "touch-action: manipulation" in html


def test_the_shops_open_to_lines_that_add_up(samples):
    x = year_of(samples).explorer
    top = x.shops[(0, 0)]
    assert top
    for shop in top + x.visits:
        assert abs(shop.lines.total - D(shop.amount.replace(",", ""))) < D("0.01"), shop.name
        assert shop.lines.count == shop.count
        assert [b.month for b in shop.strip] == list(range(1, 13))
        assert sum(b.count for b in shop.strip) == shop.count
    assert all(v.count >= 2 for v in x.visits)  # once is not going back


# --- the e-mail --------------------------------------------------------------------


def test_the_email_is_made_for_ios_mail(samples, monkeypatch):
    monkeypatch.setenv("AUTOBILL_REVISION", "15fe0d7e7a0b0e3a4139399458d0e06e74ef11c4")
    html = render_year_html(year_of(samples))
    assert 'name="format-detection" content="telephone=no, date=no' in html
    assert 'name="color-scheme" content="light dark"' in html
    assert "x-apple-data-detectors" in html and "prefers-color-scheme: dark" in html  # _email.css
    assert "<img" not in html and "href=" not in html and "http" not in html
    assert "<script" not in html
    assert '<div class="preheader">全年消费 ¥' in html
    for heading in ("每个月", "花在哪些地方", "花得最多的商户", "用哪些货币消费", "各张卡"):
        assert f'<div class="sh">{heading}</div>' in html, heading
    assert 'aria-label="2026 年每个月的消费（人民币）：1月 无账单' in html
    assert "按交易日期归到每个月" in html
    assert f"版本 {__version__} (15fe0d7)" in html


def test_the_plain_text_says_it_too(samples):
    text = year_plain_text(year_of(samples))
    assert text.startswith("2026 年度回顾\n")
    for line in ("全年消费：¥", "每个月：1月 无消费", "分类：", "花得最多的商户：", "生成于 "):
        assert line in text, line


# --- when it goes out: once, with the January that completes the year --------------


def send(conn, fx, today, factory=FakeSMTP):
    return send_year_review(conn, Mailer(SMTP, "secret", factory), fx, today=today)


def test_the_review_goes_out_with_the_complete_january_after_the_year(samples):
    conn, fx, _ = samples
    record_sent(conn, "2027-01", "<january@example.invalid>", complete=True)
    assert send(conn, fx, date(2027, 1, 15)) == (2026, None)
    (msg,) = sent_messages()
    assert msg["Subject"] == "📊 2026 年信用卡年度回顾" and msg["X-AutoBill-Report"] == "true"
    assert list(msg.iter_attachments()) == [] and msg["In-Reply-To"] is None
    assert "全年消费：¥" in msg.get_body(("plain",)).get_content()
    stored = conn.execute("SELECT year, message_id FROM year_reviews").fetchall()
    assert [tuple(r) for r in stored] == [(2026, msg["Message-ID"])]
    assert send(conn, fx, date(2027, 1, 20)) == (None, None)  # once a year
    assert len(sent_messages()) == 1


def test_nothing_goes_out_before_january_is_complete(samples):
    conn, fx, _ = samples
    assert send(conn, fx, date(2027, 1, 15)) == (None, None)  # January not sent at all
    record_sent(conn, "2027-01", "<january@example.invalid>", complete=False)
    assert send(conn, fx, date(2027, 1, 15)) == (None, None)  # sent, still waiting for cards
    assert sent_messages() == []


def test_a_year_long_past_is_never_reviewed(samples):
    """Only in the next year's first quarter: the history imported in September 2026 has a
    complete January 2026, and 2025 must not get a review nine months late."""
    conn, _, _ = samples
    record_sent(conn, "2026-01", "<january@example.invalid>", complete=True)
    assert due_year(conn, date(2026, 9, 29)) is None
    assert due_year(conn, date(2026, 3, 31)) == 2025  # still in time on the last day
    record_sent(conn, "2027-01", "<january@example.invalid>", complete=True)
    assert due_year(conn, date(2027, 4, 1)) is None


def test_a_failed_review_is_tried_again_next_run(samples):
    conn, fx, _ = samples
    record_sent(conn, "2027-01", "<january@example.invalid>", complete=True)

    def broken(*args, **kwargs):
        return FakeSMTP(*args, **kwargs, fail_on_send=True)

    year, error = send(conn, fx, date(2027, 1, 15), factory=broken)
    assert year == 2026 and "SMTPServerDisconnected" in error
    assert conn.execute("SELECT COUNT(*) FROM year_reviews").fetchone()[0] == 0
    assert send(conn, fx, date(2027, 1, 16)) == (2026, None)


# --- the commands --------------------------------------------------------------------


@pytest.fixture
def mail_config(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(fx_module, "http_fetch", FakeFrankfurter(RATES))
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    monkeypatch.setenv("AUTOBILL_IMAP_PASSWORD", "app-password")
    (isolated_data_dir / "config.yaml").write_text(
        "notifier:\n  smtp_report:\n    enabled: true\n    smtp_server: smtp.example.invalid\n"
        "    smtp_port: 587\n    security: starttls\n    username: bills@example.invalid\n"
        "    to_addr: me@example.invalid\n",
        encoding="utf-8",
    )
    conn = connect(isolated_data_dir / "autobill.db")
    load_samples(conn, isolated_data_dir)
    return conn


def test_year_review_prints_the_year_or_writes_it_as_html(mail_config, tmp_path):
    result = runner.invoke(app, ["year-review", "--year", "2026"])
    assert result.exit_code == 0, result.output
    assert "2026 年度回顾" in result.output and "全年消费：¥" in result.output
    out = tmp_path / "year.html"
    result = runner.invoke(app, ["year-review", "--year", "2026", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert sent_messages() == []  # neither sends anything
    assert runner.invoke(app, ["year-review", "--year", "2019"]).exit_code == 2  # no spending


def test_year_review_send_mails_it_now_without_counting_as_sent(mail_config):
    result = runner.invoke(app, ["year-review", "--year", "2026", "--send"])
    assert result.exit_code == 0, result.output
    (msg,) = sent_messages()
    assert msg["Subject"] == "📊 2026 年信用卡年度回顾"
    assert mail_config.execute("SELECT COUNT(*) FROM year_reviews").fetchone()[0] == 0


def test_every_run_looks_for_the_review_after_the_months(mail_config, monkeypatch):
    """_send_reports is what `run` and `serve` send with: it must go on to the review."""
    calls = []
    monkeypatch.setattr(
        "autobill.cli.send_year_review", lambda *args, **kwargs: calls.append(1) or (None, None)
    )
    _send_reports(mail_config)
    assert calls == [1]


# --- the columns: twelve fit where six did -----------------------------------------


def bar_widths(svg: str) -> set[float]:
    """Each column's width, from its path: M x,base ... H .. Q x+width,top ..."""
    pairs = re.findall(
        r'd="M([\d.]+),\d+ V[\d.]+ Q[\d.]+,[\d.]+ [\d.]+,[\d.]+ H[\d.]+ Q([\d.]+),', svg
    )
    return {round(float(right) - float(left), 1) for left, right in pairs}


def test_twelve_columns_are_narrower_six_stay_as_they_were():
    six = [MonthBar(f"2026-{m:02d}", D(100 * m)) for m in range(4, 10)]
    twelve = [MonthBar(f"2026-{m:02d}", D(100 * m)) for m in range(1, 13)]
    assert bar_widths(trend_svg(six)) == {40.0}
    assert bar_widths(trend_svg(twelve)) == {28.0}
    assert 'aria-label="近 6 个月消费（人民币）：' in trend_svg(six)
    assert 'aria-label="2026 年（人民币）：' in trend_svg(twelve, "2026 年")
