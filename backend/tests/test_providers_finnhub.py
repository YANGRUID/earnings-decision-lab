import re
from datetime import date
from decimal import Decimal

import pytest

from providers.finnhub import FinnhubEarningsCalendarProvider, FinnhubError

CALENDAR_URL_PATTERN = re.compile(r"https://finnhub\.io/api/v1/calendar/earnings.*")
PROFILE_URL_PATTERN = re.compile(r"https://finnhub\.io/api/v1/stock/profile2.*")

# Real-shaped /calendar/earnings response per Finnhub's documented schema.
REAL_CALENDAR_RESPONSE = {
    "earningsCalendar": [
        {
            "date": "2026-08-25",
            "epsActual": None,
            "epsEstimate": 2.29,
            "hour": "amc",
            "quarter": 3,
            "revenueActual": None,
            "revenueEstimate": 89400000000,
            "symbol": "AAPL",
            "year": 2026,
        },
        {
            "date": "2026-08-26",
            "epsActual": None,
            "epsEstimate": None,
            "hour": "bmo",
            "quarter": 2,
            "revenueActual": None,
            "revenueEstimate": None,
            "symbol": "WMT",
            "year": 2026,
        },
        {
            "date": "2026-08-27",
            "epsActual": None,
            "epsEstimate": 1.10,
            "hour": "",
            "quarter": 3,
            "revenueActual": None,
            "revenueEstimate": None,
            "symbol": "ZZTEST",
            "year": 2026,
        },
        # Malformed entry -- missing symbol -- must be skipped, never crash
        # the whole batch.
        {"date": "2026-08-28", "hour": "amc"},
        # Malformed entry -- unparseable date -- must be skipped too.
        {"symbol": "BADCO", "date": "not-a-date", "hour": "amc"},
    ]
}

REAL_PROFILE_RESPONSE = {
    "country": "US",
    "currency": "USD",
    "exchange": "NASDAQ NMS - GLOBAL MARKET",
    "ipo": "1980-12-12",
    "marketCapitalization": 3450000,
    "name": "Apple Inc",
    "phone": "14089961010",
    "shareOutstanding": 15550.06,
    "ticker": "AAPL",
    "weburl": "https://www.apple.com/",
    "logo": "https://static2.finnhub.io/file/publicdatany/finnhubimage/stock_logo/AAPL.png",
    "finnhubIndustry": "Technology",
}


def test_requires_api_key():
    with pytest.raises(ValueError):
        FinnhubEarningsCalendarProvider(api_key="")


def test_get_earnings_calendar_parses_real_response(httpx_mock):
    httpx_mock.add_response(url=CALENDAR_URL_PATTERN, json=REAL_CALENDAR_RESPONSE)
    provider = FinnhubEarningsCalendarProvider(api_key="test-key")

    entries = provider.get_earnings_calendar(date(2026, 8, 25), date(2026, 8, 28))

    # Two malformed rows (no symbol, unparseable date) are skipped, never
    # raise -- one bad row must never take down the whole batch.
    assert len(entries) == 3

    aapl = next(e for e in entries if e.symbol == "AAPL")
    assert aapl.earnings_date == date(2026, 8, 25)
    assert aapl.session == "amc"
    assert aapl.fiscal_year == 2026
    assert aapl.fiscal_quarter == 3
    assert aapl.eps_estimate == Decimal("2.29")
    assert aapl.revenue_estimate == Decimal("89400000000")
    assert aapl.source_provider == "finnhub"

    wmt = next(e for e in entries if e.symbol == "WMT")
    assert wmt.session == "bmo"
    assert wmt.eps_estimate is None  # real nulls stay null, never fabricated

    unknown_session = next(e for e in entries if e.symbol == "ZZTEST")
    assert unknown_session.session == ""  # raw Finnhub value, unmapped at this layer


def test_get_earnings_calendar_sends_real_date_range_params(httpx_mock):
    httpx_mock.add_response(url=CALENDAR_URL_PATTERN, json={"earningsCalendar": []})
    provider = FinnhubEarningsCalendarProvider(api_key="test-key")

    provider.get_earnings_calendar(date(2026, 1, 1), date(2026, 1, 31))

    request = httpx_mock.get_requests()[0]
    assert request.url.params["from"] == "2026-01-01"
    assert request.url.params["to"] == "2026-01-31"
    assert request.url.params["token"] == "test-key"


def test_get_earnings_calendar_raises_on_invalid_api_key(httpx_mock):
    httpx_mock.add_response(
        url=CALENDAR_URL_PATTERN, status_code=401, json={"error": "Invalid API key"}
    )
    provider = FinnhubEarningsCalendarProvider(api_key="bad-key")

    with pytest.raises(FinnhubError) as exc_info:
        provider.get_earnings_calendar(date(2026, 8, 25), date(2026, 8, 28))
    assert exc_info.value.rate_limited is False


def test_get_earnings_calendar_raises_rate_limited_on_429(httpx_mock):
    # Retried 3x by tenacity (all 429) -- register the response for every attempt.
    for _ in range(3):
        httpx_mock.add_response(url=CALENDAR_URL_PATTERN, status_code=429)
    provider = FinnhubEarningsCalendarProvider(api_key="test-key")

    with pytest.raises(FinnhubError) as exc_info:
        provider.get_earnings_calendar(date(2026, 8, 25), date(2026, 8, 28))
    assert exc_info.value.rate_limited is True


def test_get_earnings_calendar_raises_on_unexpected_shape(httpx_mock):
    httpx_mock.add_response(url=CALENDAR_URL_PATTERN, json={"unexpected": "shape"})
    provider = FinnhubEarningsCalendarProvider(api_key="test-key")

    with pytest.raises(FinnhubError):
        provider.get_earnings_calendar(date(2026, 8, 25), date(2026, 8, 28))


def test_get_company_profile_parses_real_response(httpx_mock):
    httpx_mock.add_response(url=PROFILE_URL_PATTERN, json=REAL_PROFILE_RESPONSE)
    provider = FinnhubEarningsCalendarProvider(api_key="test-key")

    profile = provider.get_company_profile("AAPL")

    assert profile is not None
    assert profile.symbol == "AAPL"
    assert profile.name == "Apple Inc"
    assert profile.logo_url == REAL_PROFILE_RESPONSE["logo"]
    assert profile.exchange == "NASDAQ NMS - GLOBAL MARKET"
    assert profile.country == "US"
    assert profile.market_cap_millions == Decimal("3450000")
    assert profile.currency == "USD"


def test_get_company_profile_returns_none_for_unknown_symbol(httpx_mock):
    # Finnhub's real behavior: HTTP 200 with an empty object, not a 404.
    httpx_mock.add_response(url=PROFILE_URL_PATTERN, json={})
    provider = FinnhubEarningsCalendarProvider(api_key="test-key")

    assert provider.get_company_profile("ZZNOTREAL") is None


def test_get_company_profile_raises_on_invalid_api_key(httpx_mock):
    httpx_mock.add_response(url=PROFILE_URL_PATTERN, status_code=401)
    provider = FinnhubEarningsCalendarProvider(api_key="bad-key")

    with pytest.raises(FinnhubError):
        provider.get_company_profile("AAPL")
