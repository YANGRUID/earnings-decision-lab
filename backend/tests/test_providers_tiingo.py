from datetime import date
from decimal import Decimal

import pytest

from providers.tiingo import TiingoMarketDataProvider

RESPONSE = [
    {
        "date": "2025-09-22T00:00:00.000Z",
        "close": 111.20,
        "high": 112.00,
        "low": 109.80,
        "open": 110.50,
        "volume": 18234500,
    },
    {
        "date": "2025-09-23T00:00:00.000Z",
        "close": 118.75,
        "high": 120.00,
        "low": 110.50,
        "open": 111.00,
        "volume": 42891200,
    },
]


def test_requires_api_key():
    with pytest.raises(ValueError):
        TiingoMarketDataProvider(api_key="")


def test_get_daily_bars_parses_response(httpx_mock):
    httpx_mock.add_response(
        url="https://api.tiingo.com/tiingo/daily/mu/prices",
        match_params={
            "startDate": "2025-09-22",
            "endDate": "2025-09-23",
            "token": "test-key",
            "format": "json",
        },
        json=RESPONSE,
    )
    provider = TiingoMarketDataProvider(api_key="test-key")

    bars = provider.get_daily_bars("MU", date(2025, 9, 22), date(2025, 9, 23))

    assert len(bars) == 2
    assert bars[1].trade_date == date(2025, 9, 23)
    assert bars[1].close == Decimal("118.75")
    assert bars[0].source_provider == "tiingo"
