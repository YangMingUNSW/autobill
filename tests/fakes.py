"""Test doubles shared by several test modules."""

import json
from datetime import date

import httpx


class FakeFrankfurter:
    """Answers like api.frankfurter.dev: weekends map to Friday, unknown days are 404."""

    def __init__(self, rates: dict[tuple[str, str], str], down: bool = False):
        self.rates = rates  # (currency, published day) -> rate
        self.down = down
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if self.down:
            raise httpx.ConnectError("offline")
        day = date.fromisoformat(url.rsplit("/", 1)[1].split("?")[0])
        currency = url.split("base=")[1].split("&")[0]
        while day.weekday() >= 5:  # Saturday/Sunday -> previous Friday
            day = date.fromordinal(day.toordinal() - 1)
        rate = self.rates.get((currency, day.isoformat()))
        if rate is None:
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404))
        return json.dumps(
            {"amount": 1.0, "base": currency, "date": day.isoformat(), "rates": {"CNY": rate}}
        ).replace(f'"{rate}"', rate)
