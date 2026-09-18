"""Exchange rates without any network access: a fake fetch function stands in for HTTP."""

from datetime import date
from decimal import Decimal

import pytest
from fakes import FakeFrankfurter

from autobill.config import FxConfig, load_config
from autobill.fx import FxRates, RateUnavailable
from autobill.store.db import connect


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "fx.db")


RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109"}


def test_cny_needs_no_lookup(conn):
    fake = FakeFrankfurter({})
    rate = FxRates(conn, FxConfig(), fake).rate("CNY", date(2026, 9, 2))
    assert rate.rate_to_cny == Decimal("1") and fake.calls == []


def test_fetches_once_then_uses_cache(conn):
    fake = FakeFrankfurter(RATES)
    fx = FxRates(conn, FxConfig(), fake)
    first = fx.rate("USD", date(2026, 9, 2))
    again = fx.rate("USD", date(2026, 9, 2))
    assert first == again
    assert first.rate_to_cny == Decimal("6.7215") and first.source == "frankfurter"
    assert len(fake.calls) == 1
    assert "2026-09-02?base=USD&symbols=CNY" in fake.calls[0]


def test_rate_is_exact_decimal(conn):
    rate = FxRates(conn, FxConfig(), FakeFrankfurter(RATES)).rate("USD", date(2026, 9, 2))
    assert str(rate.rate_to_cny) == "6.7215"  # parsed as Decimal, never through float


def test_weekend_uses_previous_working_day(conn):
    fx = FxRates(conn, FxConfig(), FakeFrankfurter(RATES))
    rate = fx.rate("USD", date(2026, 9, 6))  # a Sunday
    assert rate.rate_date == date(2026, 9, 4) and rate.rate_to_cny == Decimal("6.7109")


def test_unpublished_day_steps_back(conn):
    fake = FakeFrankfurter(RATES)
    rate = FxRates(conn, FxConfig(), fake).rate("USD", date(2026, 9, 3))  # 404, then 09-02
    assert rate.rate_date == date(2026, 9, 2)
    assert len(fake.calls) == 2


def test_network_down_uses_config_fallback_without_caching(conn):
    fx = FxRates(conn, FxConfig(), FakeFrankfurter(RATES, down=True))
    rate = fx.rate("USD", date(2026, 9, 2))
    assert (rate.rate_to_cny, rate.source, rate.rate_date) == (Decimal("7.10"), "config", None)
    assert conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0
    # Next run with the network back gets the real rate.
    fx.fetch = FakeFrankfurter(RATES)
    assert fx.rate("USD", date(2026, 9, 2)).source == "frankfurter"


def test_no_rate_anywhere(conn):
    fx = FxRates(conn, FxConfig(fallback_to_cny={}), FakeFrankfurter({}, down=True))
    with pytest.raises(RateUnavailable):
        fx.rate("GBP", date(2026, 9, 2))


def test_config_file_fallback_is_exact(isolated_data_dir):
    (isolated_data_dir / "config.yaml").write_text(
        "fx:\n  fallback_to_cny: { USD: 7.1, AUD: 4.65 }\nsystem: { timezone: Asia/Shanghai }\n",
        encoding="utf-8",
    )
    fallback = load_config().fx.fallback_to_cny
    assert fallback == {"USD": Decimal("7.1"), "AUD": Decimal("4.65")}
    assert str(fallback["AUD"]) == "4.65"  # not 4.6500000000000003552713678800500929355621337890625


def test_missing_config_uses_example_defaults():
    assert load_config().fx.fallback_to_cny["USD"] == Decimal("7.10")
