"""Exchange rates to CNY. See docs/data-model.md#汇率.

Order: fx_rates cache -> Frankfurter for the bill's e-mail date -> config fallback.
Frankfurter results are cached forever; config fallbacks are never cached, so the next
run tries the network again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import httpx

from autobill.config import FxConfig
from autobill.store.db import now

# fetch(url) -> response body; raises on network errors or non-2xx. Tests pass a fake.
Fetch = Callable[[str], str]
LOOKBACK_DAYS = 7  # the day's rate may not be published yet (or the date is in the future)


@dataclass(frozen=True)
class Rate:
    currency: str
    rate_to_cny: Decimal
    rate_date: date | None  # None for config fallbacks
    source: str  # "identity" / "frankfurter" / "config"


class RateUnavailable(Exception):
    pass


def http_fetch(url: str) -> str:
    response = httpx.get(url, timeout=10, follow_redirects=True)
    response.raise_for_status()
    return response.text


class FxRates:
    def __init__(self, conn: sqlite3.Connection, config: FxConfig, fetch: Fetch | None = None):
        self.conn = conn
        self.config = config
        self.fetch = fetch or http_fetch  # looked up now, so tests can replace it

    def rate(self, currency: str, on: date) -> Rate:
        """Rate from `currency` to CNY for the day `on` (a bill's email_date)."""
        if currency == "CNY":
            return Rate("CNY", Decimal("1"), on, "identity")
        cached = self.conn.execute(
            "SELECT rate_to_cny, rate_date, source FROM fx_rates WHERE date = ? AND currency = ?",
            (on.isoformat(), currency),
        ).fetchone()
        if cached:
            return Rate(
                currency,
                Decimal(cached["rate_to_cny"]),
                date.fromisoformat(cached["rate_date"]),
                cached["source"],
            )
        try:
            rate = self._from_frankfurter(currency, on)
        except (httpx.HTTPError, OSError, ValueError, KeyError, RateUnavailable):
            fallback = self.config.fallback_to_cny.get(currency)
            if fallback is None:
                raise RateUnavailable(f"no rate for {currency} on {on}") from None
            return Rate(currency, fallback, None, "config")
        self.conn.execute(
            """INSERT OR REPLACE INTO fx_rates
                   (date, currency, rate_to_cny, rate_date, source, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                on.isoformat(),
                currency,
                str(rate.rate_to_cny),
                rate.rate_date.isoformat(),
                rate.source,
                now(),
            ),
        )
        return rate

    def _from_frankfurter(self, currency: str, on: date) -> Rate:
        """Frankfurter answers weekends with the previous working day; a day that is not
        published yet (or in the future) is 404, so step back a few days."""
        base = self.config.source.rstrip("/")
        last_error: Exception | None = None
        for back in range(LOOKBACK_DAYS):
            day = on - timedelta(days=back)
            url = f"{base}/{day.isoformat()}?base={currency}&symbols=CNY"
            try:
                body = json.loads(self.fetch(url), parse_float=Decimal)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    last_error = exc
                    continue
                raise
            return Rate(
                currency,
                Decimal(body["rates"]["CNY"]),
                date.fromisoformat(body["date"]),
                "frankfurter",
            )
        raise RateUnavailable(f"Frankfurter has no {currency} rate near {on}") from last_error
