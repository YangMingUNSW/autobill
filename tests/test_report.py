"""Monthly summary on the three ABC samples. Expected figures were recomputed from the raw
HTML by a separate script (not through the parser) and match docs/notify.md#统计口径."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fakes import FakeFrankfurter

from autobill.config import FxConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates, Rate
from autobill.pipeline import process
from autobill.report.monthly import Line, MonthlySummary, month_bounds, monthly_summary, render_text
from autobill.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
D = Decimal
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109"}


@pytest.fixture
def summary_for(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    for mail in DirectorySource(FIXTURES / "abc").iter_new():
        process(conn, isolated_data_dir, mail)
    fx = FxRates(conn, FxConfig(), FakeFrankfurter(RATES))
    return lambda month: monthly_summary(conn, month, fx)


def test_august(summary_for):
    s = summary_for("2026-08")
    lines = {(line.account_id, line.currency): line for line in s.lines}
    # MC: purchases 28.25 + 28.64, minus two 0.28 rebates
    assert lines[("ABC:0001", "USD")].spend == D("56.33")
    assert lines[("ABC:0001", "USD")].cny == D("378.62")  # 56.33 x 6.7215 (e-mail 09-02)
    assert lines[("ABC:0002", "USD")].spend == D("2249.67")
    assert lines[("ABC:0002", "USD")].rate.rate_date == date(2026, 9, 4)
    # Union Pay: two 财付通 payments on 08-31; the 659.00 instalment principal is not spending
    assert lines[("ABC:0003", "CNY")].spend == D("150.00")
    assert s.by_currency == {"CNY": D("150.00"), "USD": D("2306.00")}
    assert s.total_cny == D("378.62") + D("15097.31") + D("150.00")


def test_september_includes_instalment_interest(summary_for):
    s = summary_for("2026-09")
    lines = {(line.account_id, line.currency): line.spend for line in s.lines}
    assert lines == {("ABC:0002", "USD"): D("2.60"), ("ABC:0003", "CNY"): D("409.59")}


def test_empty_month(summary_for):
    s = summary_for("2026-01")
    assert s.lines == [] and "没有支出数据" in render_text(s)


def test_render_mentions_rate_date_and_totals(summary_for):
    text = render_text(summary_for("2026-08"))
    assert "6.7215（2026-09-02）" in text
    assert "分币种合计：CNY 150.00；USD 2,306.00" in text
    assert "人民币合计：15,625.93" in text


def test_render_flags_problems():
    config_rate = Rate("USD", D("7.10"), None, "config")
    s = MonthlySummary(
        "2026-08",
        [
            Line("ABC:0001", date(2026, 9, 1), "WARN", "USD", D("10.00"), config_rate, D("71.00")),
            Line("ABC:0002", date(2026, 9, 2), "UNVERIFIED", "EUR", D("5.00"), None, None),
        ],
    )
    text = render_text(s)
    assert "配置汇率" in text and "对账有警告" in text and "未对账" in text
    assert "无汇率" in text and "没有计入人民币合计" in text
    assert s.total_cny == D("71.00")


@pytest.mark.parametrize(
    ("month", "bounds"),
    [
        ("2026-08", (date(2026, 8, 1), date(2026, 9, 1))),
        ("2026-12", (date(2026, 12, 1), date(2027, 1, 1))),
    ],
)
def test_month_bounds(month, bounds):
    assert month_bounds(month) == bounds


@pytest.mark.parametrize("bad", ["2026-13", "2026/08", "August", "2026"])
def test_month_bounds_rejects(bad):
    with pytest.raises(ValueError):
        month_bounds(bad)
