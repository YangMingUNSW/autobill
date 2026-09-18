"""Standard statements (docs/statement.md): one unified template for every bank's bill."""

import re
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fakes import FakeFrankfurter
from typer.testing import CliRunner

from autobill import fx as fx_module
from autobill.categorize import load_rules
from autobill.cli import app
from autobill.config import FxConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.pipeline import process
from autobill.report import pdf
from autobill.report.statement import build_view, render_statement_html
from autobill.store.db import connect, load_bill

FIXTURES = Path(__file__).parent / "fixtures"
D = Decimal
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109",
         ("AUD", "2026-08-24"): "4.6200", ("AUD", "2025-06-25"): "4.7000"}  # fmt: skip
NOW = datetime(2026, 9, 19, 8, 0)


@pytest.fixture
def db(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    for mail in DirectorySource(FIXTURES).iter_new():
        process(conn, isolated_data_dir, mail)
    return conn, FxRates(conn, FxConfig(), FakeFrankfurter(RATES))


def bills(conn):
    rows = conn.execute("SELECT id FROM bills ORDER BY account_id, statement_date").fetchall()
    return [load_bill(conn, r[0]) for r in rows]


def bill_of(conn, account, statement_date=None):
    return next(
        b
        for b in reversed(bills(conn))
        if b.account_id == account
        and (statement_date is None or str(b.statement_date) == statement_date)
    )


def render(conn, fx, account, statement_date=None):
    return render_statement_html(bill_of(conn, account, statement_date), fx, load_rules(), NOW)


def test_every_sample_renders_with_every_transaction(db):
    conn, fx = db
    all_bills = bills(conn)
    assert len(all_bills) == 8
    for bill in all_bills:
        html = render_statement_html(bill, fx, load_rules(), NOW)
        lines = re.findall(r'data-line="(\d+)"', html)
        assert sorted(map(int, lines)) == sorted(t.line_no for t in bill.transactions), (
            bill.account_id
        )
        assert "AutoBill 标准账单" in html and "以银行原账单为准" in html


def test_no_scripts_links_or_external_resources(db):
    conn, fx = db
    for bill in bills(conn):
        html = render_statement_html(bill, fx, load_rules(), NOW)
        assert "<script" not in html and "href=" not in html
        assert "http://" not in html and "https://" not in html and "@import" not in html


def test_account_summary_is_the_reconciliation_identity(db):
    conn, fx = db
    view = build_view(bill_of(conn, "ABC:0002"), fx, load_rules(), NOW)
    usd = next(b for b in view.balances if b.currency == "USD")
    # 2836.84 - 3.29 previous (net), + 2285.06, - 2866.34, = 2252.79 - 0.52
    assert (usd.previous, usd.charges, usd.credits, usd.new) == (
        "2,833.55",
        "2,285.06",
        "-2,866.34",
        "2,252.27",
    )
    assert D("2833.55") + D("2285.06") - D("2866.34") == D("2252.27")
    assert (usd.due, usd.minimum) == ("2,252.79", "225.29")


def test_status_badge_follows_the_bill(db):
    conn, fx = db
    assert "对账通过" in render(conn, fx, "ABC:0003")
    conn.execute("UPDATE bills SET status = 'WARN' WHERE account_id = 'ABC:0003'")
    assert "对账有警告" in render(conn, fx, "ABC:0003")


def test_union_pay_instalment_and_synthetic_adjustment(db):
    conn, fx = db
    html = render(conn, fx, "ABC:0003")
    assert "总账分期 本期分期本金 第3/36期" in html
    assert "银行未列明细，由对账补出" in html
    assert html.count("不计入支出") == 3  # repayment, instalment principal, adjustment
    view = build_view(bill_of(conn, "ABC:0003"), fx, load_rules(), NOW)
    assert view.spend_cny == "559.59"  # 460.00 purchases + 99.59 interest; principal excluded


def test_foreign_amounts_and_fx_rate(db):
    conn, fx = db
    html = render(conn, fx, "ABC:0001")
    assert "AUD 39.90" in html  # original currency of the Mastercard purchases
    assert "汇率 6.76485" in html  # automatic currency purchase
    assert "USD 按 6.7215 折算人民币" in html


def test_zero_balance_boc_statement(db):
    conn, fx = db
    view = build_view(bill_of(conn, "BOC:0005", "2026-08-22"), fx, load_rules(), NOW)
    assert view.nothing_due and view.due_date is None
    html = render(conn, fx, "BOC:0005", "2026-08-22")
    assert "无需还款" in html and "本期没有交易" in html


def test_combined_boc_cards_stay_separate_and_hide_device_numbers(db):
    conn, fx = db
    html = render(conn, fx, "BOC:0006", "2025-06-22")
    assert "尾号 0006" in html and "0008" not in html  # Apple Pay device number never shown
    assert "尾号 0005" not in html


def test_single_currency_rows_omit_the_code(db):
    conn, fx = db
    html = render(conn, fx, "BOC:0006", "2025-06-22")
    assert "金额单位 CNY" in html
    assert not re.search(r'class="amount num">CNY', html)
    multi = render(conn, fx, "ABC:0002")
    assert re.search(r'class="amount num">USD', multi)


def test_daily_chart_counts_spending_days_only(db):
    conn, fx = db
    bill = bill_of(conn, "ABC:0003")  # period 08-17 .. 09-16, spending on 4 days
    view = build_view(bill, fx, load_rules(), NOW)
    assert view.daily_caption.startswith("31 天里有 4 天有消费")
    assert str(view.daily_chart).count('class="bar"') == 4
    assert "¥200" in str(view.daily_chart)  # only the largest day is labelled


def test_boc_chart_uses_transaction_span(db):
    conn, fx = db
    view = build_view(bill_of(conn, "BOC:0005", "2025-06-22"), fx, load_rules(), NOW)
    assert "08-" not in str(view.daily_chart)  # spans May-June transaction dates
    assert "天里有" in view.daily_caption


# --- PDF -----------------------------------------------------------------------------


def test_find_browser_prefers_configured_path(tmp_path):
    exe = tmp_path / "browser.exe"
    exe.write_text("x")
    assert pdf.find_browser(str(exe)) == exe
    assert pdf.find_browser(str(tmp_path / "missing.exe")) is None


def test_html_to_pdf_command_and_success(tmp_path):
    html = tmp_path / "a.html"
    html.write_text("<p>x</p>", encoding="utf-8")
    target = tmp_path / "a.pdf"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        target.write_bytes(b"%PDF-1.7 fake")
        return subprocess.CompletedProcess(command, 0)

    pdf.html_to_pdf(html, target, Path("edge.exe"), run=fake_run, sleep=lambda s: None)
    command, kwargs = calls[0]
    assert command[0] == "edge.exe" and "--headless=new" in command
    assert f"--print-to-pdf={target.resolve()}" in command
    assert any(c.startswith("--user-data-dir=") for c in command)
    assert command[-1] == html.resolve().as_uri()
    assert kwargs["timeout"] == pdf.TIMEOUT_SECONDS


def test_html_to_pdf_waits_for_a_late_writer(tmp_path):
    html, target = tmp_path / "a.html", tmp_path / "a.pdf"
    html.write_text("x", encoding="utf-8")
    ticks = []

    def late_sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 3:  # the browser's child process finishes a moment later
            target.write_bytes(b"%PDF-1.7 late")

    pdf.html_to_pdf(html, target, Path("edge.exe"), run=lambda *a, **k: None, sleep=late_sleep)
    assert target.read_bytes().startswith(b"%PDF-")


def test_html_to_pdf_failures(tmp_path):
    html, target = tmp_path / "a.html", tmp_path / "a.pdf"
    html.write_text("x", encoding="utf-8")
    with pytest.raises(pdf.PdfError):  # nothing written
        pdf.html_to_pdf(html, target, Path("e.exe"), run=lambda *a, **k: None, sleep=lambda s: None)

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("e.exe", 1)

    with pytest.raises(pdf.PdfError):
        pdf.html_to_pdf(html, target, Path("e.exe"), run=hang, sleep=lambda s: None)


@pytest.mark.skipif(pdf.find_browser() is None, reason="no Edge/Chrome on this machine (CI)")
def test_real_browser_prints_a_pdf(db, tmp_path):
    conn, fx = db
    html = tmp_path / "s.html"
    html.write_text(render(conn, fx, "ABC:0003"), encoding="utf-8")
    pdf.html_to_pdf(html, tmp_path / "s.pdf", pdf.find_browser())
    assert (tmp_path / "s.pdf").read_bytes().startswith(b"%PDF-")


# --- CLI -----------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_env(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(fx_module, "http_fetch", FakeFrankfurter(RATES))
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES)])
    return isolated_data_dir


def test_statement_all_writes_one_file_per_bill(cli_env, tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["statement", "--all", "--no-pdf", "-o", str(out)])
    assert result.exit_code == 0, result.output
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.html"))
    assert len(files) == 8 and "ABC-0003/2026-09-16.html" in files
    assert "BOC-0006/2025-06-22.html" in files


def test_statement_single_card_newest_only(cli_env, tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["statement", "--account", "CCB:0004", "--no-pdf", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert [p.name for p in out.rglob("*.html")] == ["2026-07-10.html"]


def test_statement_requires_a_target(cli_env):
    assert runner.invoke(app, ["statement"]).exit_code != 0


def test_statement_without_browser_writes_html_only(cli_env, tmp_path, monkeypatch):
    monkeypatch.setattr("autobill.cli.find_browser", lambda configured=None: None)
    out = tmp_path / "out"
    result = runner.invoke(app, ["statement", "--account", "ABC:0003", "-o", str(out)])
    assert result.exit_code == 0 and "没有找到 Edge 或 Chrome" in result.output
    assert list(out.rglob("*.pdf")) == [] and len(list(out.rglob("*.html"))) == 1


def test_credits_are_shown_negative(db):
    conn, fx = db
    html = render(conn, fx, "ABC:0003")
    assert re.search(r'class="amount num">-1,209\.28<', html)  # the repayment
    assert re.search(r'class="amount num">-0\.62<', html)  # the synthetic adjustment


def test_single_card_rows_do_not_repeat_the_card(db):
    conn, fx = db
    view = build_view(bill_of(conn, "ABC:0002"), fx, load_rules(), NOW)
    assert not any("尾号" in m for day in view.days for line in day.lines for m in line.meta)


def test_daily_chart_ignores_rebates(db):
    conn, fx = db
    view = build_view(bill_of(conn, "ABC:0002"), fx, load_rules(), NOW)
    # purchases only; the 50 small rebates must not lower any day's bar
    assert view.daily_caption == "31 天里有 28 天有消费，最多的一天 ¥1,778.12"
