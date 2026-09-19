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


# --- M6: categories, cards, trend, merchants ---------------------------------------


def test_categories_and_credits(summary_for):
    s = summary_for("2026-08")
    # Recomputed from the raw HTML by a separate script (not through the parser).
    # Woolworths 3588.72, plus (M7d trade words) MAPLE SUPERMARKET x3, KPAY*CITY CONVENIENCE and
    # LUCKY ASIAN MARKET: USD 252.17 at 6.7109 = 1692.29.
    assert s.categories["超市"] == D("3588.72") + D("1692.29")
    assert s.categories["微信/支付宝（未细分）"] == D("150.00")  # the two 财付通 payments
    assert s.credits == {"返现": D("-220.32")}
    assert list(s.categories.values()) == sorted(s.categories.values(), reverse=True)
    # Categories minus credits equal the total, up to rounding of each item.
    total = sum(s.categories.values()) + sum(s.credits.values())
    assert abs(total - s.total_cny) <= D("0.05")


def test_by_account_and_trend(summary_for):
    s = summary_for("2026-08")
    assert s.by_account == {
        "ABC:0001": D("378.62"),
        "ABC:0002": D("15097.31"),
        "ABC:0003": D("150.00"),
    }
    assert [m for m, _ in s.trend] == [
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
    ]
    assert s.trend[-1] == ("2026-08", s.total_cny)
    assert s.trend[0][1] is None  # no data that month


def test_top_and_uncategorised_merchants(summary_for):
    s = summary_for("2026-08")
    assert len(s.top_merchants) == 10
    assert s.top_merchants[0][0] == "Woolworths Online"
    values = [v for _, v in s.top_merchants]
    assert values == sorted(values, reverse=True)
    names = {name for name, _, _ in s.uncategorised}
    assert "Woolworths Online" not in names and "HARBOUR FISH PTY LTD" in names
    assert "OLIVE GREEK TAVERNA" not in names  # the trade word TAVERNA catches it (M7d)


def test_editing_rules_takes_effect_without_reimport(summary_for, isolated_data_dir):
    before = summary_for("2026-08")
    assert "HARBOUR FISH PTY LTD" in {n for n, _, _ in before.uncategorised}
    (isolated_data_dir / "rules.yaml").write_text("餐饮: [HARBOUR FISH]\n", encoding="utf-8")
    after = summary_for("2026-08")
    assert "HARBOUR FISH PTY LTD" not in {n for n, _, _ in after.uncategorised}
    added = after.categories["餐饮"] - before.categories.get("餐饮", D("0"))
    assert added == D("773.77")  # USD 36.43 + 78.87 at 6.7109, on top of the built-in rules


def test_render_has_every_section(summary_for):
    text = render_text(summary_for("2026-08"))
    for heading in [
        "一、各卡明细",
        "二、分类",
        "三、按卡",
        "四、近 6 个月",
        "五、支出最多的商户",
        "六、未分类",
    ]:
        assert heading in text
    assert "返现（扣减）" in text and "← 本月" in text
