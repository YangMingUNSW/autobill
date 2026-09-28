"""The year in review (docs/notify.md#年度回顾), offline."""

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fakes import FakeFrankfurter
from typer.testing import CliRunner

from autobill import __version__
from autobill import fx as fx_module
from autobill.categorize import load_rules
from autobill.cli import app
from autobill.config import FxConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.model import Bill, BillBalance, Transaction, TxnType, make_txn_id
from autobill.pipeline import process
from autobill.report.cycle import MonthBar, Segment, month_spend_cny, month_totals, trend_svg
from autobill.report.year import build_year_report, render_year_html, year_plain_text
from autobill.store.db import connect, save_bill

FIXTURES = Path(__file__).parent / "fixtures"
D = Decimal
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109",
         ("AUD", "2026-08-24"): "4.6200", ("AUD", "2025-06-25"): "4.7000"}  # fmt: skip
runner = CliRunner()


def load_samples(conn, data_dir):
    for mail in DirectorySource(FIXTURES).iter_new():
        process(conn, data_dir, mail)


@pytest.fixture
def samples(isolated_data_dir):
    """The anonymised sample statements: 2026 has statement months June to September,
    2025 only June."""
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


# --- the numbers: the twelve months add up to the year -----------------------------


def test_the_twelve_months_add_up_to_the_year(samples):
    """Counted exactly as the months' own e-mails count 本月消费, so they add up."""
    conn, fx, rules = samples
    report = year_of(samples)
    months = [month_spend_cny(conn, f"2026-{m:02d}", fx, rules) for m in range(1, 13)]
    assert [b.value for b in report.bars] == months
    assert [b.label for b in report.bars] == [f"{m}月" for m in range(1, 13)]
    assert report.spend == sum((v for v in months if v is not None), D(0))
    assert report.months == sum(1 for v in months if v is not None)
    assert report.months >= 2


def test_the_categories_are_the_months_categories_added_up(samples):
    conn, fx, rules = samples
    report = year_of(samples)
    summed: dict[str, Decimal] = {}
    for m in range(1, 13):
        totals = month_totals(conn, f"2026-{m:02d}", fx, rules)
        for name, value in (totals.categories if totals else {}).items():
            summed[name] = summed.get(name, D(0)) + value
    assert {s.name: s.weight for s in report.categories} == {
        k: v for k, v in summed.items() if v > 0
    }
    tones = [s.tone for s in report.categories if s.tone != "none"]
    assert tones[:4] == ["s1", "s2", "s3", "s4"][: len(tones)]
    assert set(tones[4:]) <= {"other"}  # beyond the donut's four colours, grey


def test_the_biggest_month_is_the_one_in_the_accent(samples):
    report = year_of(samples)
    current = [b for b in report.bars if b.current]
    assert len(current) == 1
    assert current[0].value == max(b.value for b in report.bars if b.value is not None)
    assert report.trend_caption.startswith(f"最多：{current[0].label} ¥")
    assert report.span.endswith(f"{report.months} 个账单月")


def test_charges_are_the_interest_and_fee_categories(samples):
    """The same numbers as the category rows: a fee the rules file under 手续费 counts
    there even when the bank typed it as a purchase."""
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
    fx = FxRates(conn, FxConfig(), FakeFrankfurter({}))
    report = build_year_report(conn, 2026, fx, load_rules(conn))
    assert report.charges == D("35.00")  # by type alone it would be 5.00


def test_a_year_is_compared_only_with_a_complete_year_before(samples):
    report = year_of(samples)
    assert report.previous is None and report.change == ""  # 2025 has one statement month
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
    fx = FxRates(conn, FxConfig(), FakeFrankfurter({}))
    report = build_year_report(conn, 2026, fx, load_rules(conn))
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
    fx = FxRates(conn, FxConfig(), FakeFrankfurter({("USD", "2026-05-01"): "7.0"}))
    report = build_year_report(conn, 2026, fx, load_rules(conn))
    assert [(c.name, c.amount, c.note) for c in report.currencies] == [
        ("澳元", "70.00", "A$15.00"),
        ("人民币", "30.00", ""),
    ]
    assert report.rebates == D("7.0")  # positive, and already taken off the spending
    assert report.spend == D("93.0")


def segments(n):
    return [Segment(f"类{i}", "1.00", "1%", D(n - i), "other") for i in range(n)]


def test_the_long_tail_of_categories_folds_away(samples):
    report = year_of(samples)
    report.categories = segments(9)  # folding one row away would save nothing
    assert report.shown_categories == report.categories and report.folded_categories == []
    assert 'id="more-categories"' not in render_year_html(report)
    report.categories = segments(12)
    assert len(report.shown_categories) == 8 and len(report.folded_categories) == 4
    assert (report.folded_amount, report.folded_share) == ("10.00", "13%")  # 4+3+2+1 of 78
    html = render_year_html(report)
    assert 'id="more-categories"' in html and "其余 4 类" in html
    assert re.search(r'<input type="checkbox" id="more-categories" class="acc">\s*<label', html)


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
    assert 'aria-label="2026 年每个账单月的消费（人民币）：1月 无账单' in html
    assert f"版本 {__version__} (15fe0d7)" in html


def test_the_plain_text_says_it_too(samples):
    text = year_plain_text(year_of(samples))
    assert text.startswith("2026 年度回顾\n")
    for line in ("全年消费：¥", "每个月：1月 无账单", "分类：", "花得最多的商户：", "生成于 "):
        assert line in text, line


def test_year_review_prints_the_year_or_writes_it_as_html(isolated_data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(fx_module, "http_fetch", FakeFrankfurter(RATES))
    conn = connect(isolated_data_dir / "autobill.db")
    load_samples(conn, isolated_data_dir)
    conn.close()
    result = runner.invoke(app, ["year-review", "--year", "2026"])
    assert result.exit_code == 0, result.output
    assert "2026 年度回顾" in result.output and "全年消费：¥" in result.output
    out = tmp_path / "year.html"
    result = runner.invoke(app, ["year-review", "--year", "2026", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert runner.invoke(app, ["year-review", "--year", "2019"]).exit_code == 2  # no statements


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
