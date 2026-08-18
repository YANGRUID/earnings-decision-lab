import re
from datetime import date
from decimal import Decimal

import pytest

from providers.alpha_vantage import AlphaVantageError, AlphaVantageMarketDataProvider

RESPONSE = {
    "Meta Data": {"2. Symbol": "MU"},
    "Time Series (Daily)": {
        "2025-09-23": {
            "1. open": "111.00",
            "2. high": "120.00",
            "3. low": "110.50",
            "4. close": "118.75",
            "5. volume": "42891200",
        },
        "2025-09-22": {
            "1. open": "110.50",
            "2. high": "112.00",
            "3. low": "109.80",
            "4. close": "111.20",
            "5. volume": "18234500",
        },
        "2020-01-02": {  # outside requested range — must be filtered out
            "1. open": "1",
            "2. high": "1",
            "3. low": "1",
            "4. close": "1",
            "5. volume": "1",
        },
    },
}

RATE_LIMIT_RESPONSE = {
    "Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."
}


def test_requires_api_key():
    with pytest.raises(ValueError):
        AlphaVantageMarketDataProvider(api_key="")


def test_get_daily_bars_parses_and_filters_by_range(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://www\.alphavantage\.co/query.*"), json=RESPONSE
    )
    provider = AlphaVantageMarketDataProvider(api_key="test-key")

    bars = provider.get_daily_bars("MU", date(2025, 9, 22), date(2025, 9, 23))

    assert [b.trade_date for b in bars] == [date(2025, 9, 22), date(2025, 9, 23)]
    assert bars[1].close == Decimal("118.75")
    assert bars[0].source_provider == "alpha_vantage"


def test_rate_limit_note_raises(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://www\.alphavantage\.co/query.*"), json=RATE_LIMIT_RESPONSE
    )
    provider = AlphaVantageMarketDataProvider(api_key="test-key")

    with pytest.raises(AlphaVantageError) as exc_info:
        provider.get_daily_bars("MU", date(2025, 9, 22), date(2025, 9, 23))
    assert exc_info.value.rate_limited is True
