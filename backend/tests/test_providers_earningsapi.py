import re
from datetime import date
from decimal import Decimal

import pytest

from providers.earningsapi import EarningsApiCalendarProvider, EarningsApiError

CALENDAR_URL_PATTERN = re.compile(r"https://api\.earningsapi\.com/v1/calendar/earnings.*")
PROFILE_URL_PATTERN = re.compile(r"https://api\.earningsapi\.com/v1/profile/.*")

# Real-shaped /v1/calendar/earnings response -- confirmed live 2026-08-22
# against a real key (date=2026-08-26, NVDA's real entry).
REAL_CALENDAR_RESPONSE = {
    "date": "2026-08-26",
    "pre": [
        {
            "symbol": "WMT",
            "name": "Walmart Inc",
            "epsEstimate": 0.75,
            "eps": None,
            "revenue": None,
            "revenueEstimate": 178500000000,
        },
    ],
    "after": [
        {
            "symbol": "NVDA",
            "name": "NVIDIA Corporation",
            "epsEstimate": 2.09,
            "eps": None,
            "revenue": None,
            "revenueEstimate": 92072420560,
        },
        # Malformed entry -- missing symbol -- must be skipped, never
        # crash the whole batch.
        {"name": "No Symbol Co", "epsEstimate": 1.0},
    ],
    "notSupplied": [
        {
            "symbol": "ZZTEST",
            "name": "Unknown Timing Co",
            "epsEstimate": None,
            "eps": None,
            "revenue": None,
            "revenueEstimate": None,
        },
    ],
}

# Real-shaped /v1/profile/{symbol} response -- confirmed live for NVDA.
REAL_PROFILE_RESPONSE = {
    "symbol": "NVDA",
    "companyName": "NVIDIA CORP",
    "cik": "0001045810",
    "exchange": "NASDAQ",
    "outstandingShares": 24221000000,
    "price": 208.48,
    "marketCap": 5049594080000,
    "sector": "Technology",
    "industry": "Semiconductors",
    "country": "United States",
    "countryEmoji": "\U0001f1fa\U0001f1f8",
    "type": "equity",
    "tags": [],
}


def test_requires_api_key():
    with pytest.raises(ValueError):
        EarningsApiCalendarProvider(api_key="")


class TestGetEarningsCalendar:
    def test_parses_real_response_for_a_single_day(self, httpx_mock):
        httpx_mock.add_response(url=CALENDAR_URL_PATTERN, json=REAL_CALENDAR_RESPONSE)
        provider = EarningsApiCalendarProvider(api_key="test-key")

        entries = provider.get_earnings_calendar(date(2026, 8, 26), date(2026, 8, 26))

        # One malformed row (no symbol) is skipped, never raises.
        assert len(entries) == 3

        nvda = next(e for e in entries if e.symbol == "NVDA")
        assert nvda.earnings_date == date(2026, 8, 26)
        assert nvda.session == "amc"  # "after" bucket
        assert nvda.eps_estimate == Decimal("2.09")
        assert nvda.revenue_estimate == Decimal("92072420560")
        assert nvda.source_provider == "earningsapi"

        wmt = next(e for e in entries if e.symbol == "WMT")
        assert wmt.session == "bmo"  # "pre" bucket

        unknown_timing = next(e for e in entries if e.symbol == "ZZTEST")
        assert unknown_timing.session == ""  # "notSupplied" bucket
        assert unknown_timing.eps_estimate is None  # real nulls stay null

    def test_makes_one_real_call_per_calendar_day_not_a_range_call(self, httpx_mock):
        """EarningsAPI.com has no from/to range endpoint (confirmed live)
        -- a 3-day request must be exactly 3 real HTTP calls, one per
        real date, each with its own ``date=`` param."""
        empty_day = {"pre": [], "after": [], "notSupplied": []}
        for day in ("2026-08-24", "2026-08-25", "2026-08-26"):
            httpx_mock.add_response(url=CALENDAR_URL_PATTERN, json={"date": day, **empty_day})
        provider = EarningsApiCalendarProvider(api_key="test-key")

        provider.get_earnings_calendar(date(2026, 8, 24), date(2026, 8, 26))

        requests = httpx_mock.get_requests()
        assert len(requests) == 3
        assert [r.url.params["date"] for r in requests] == [
            "2026-08-24",
            "2026-08-25",
            "2026-08-26",
        ]
        assert all(r.url.params["apikey"] == "test-key" for r in requests)

    def test_raises_on_invalid_api_key(self, httpx_mock):
        httpx_mock.add_response(
            url=CALENDAR_URL_PATTERN, status_code=401, json={"error": "Invalid API key"}
        )
        provider = EarningsApiCalendarProvider(api_key="bad-key")

        with pytest.raises(EarningsApiError) as exc_info:
            provider.get_earnings_calendar(date(2026, 8, 26), date(2026, 8, 26))
        assert exc_info.value.rate_limited is False

    def test_raises_rate_limited_on_429(self, httpx_mock):
        for _ in range(3):  # retried 3x by tenacity, all 429
            httpx_mock.add_response(url=CALENDAR_URL_PATTERN, status_code=429)
        provider = EarningsApiCalendarProvider(api_key="test-key")

        with pytest.raises(EarningsApiError) as exc_info:
            provider.get_earnings_calendar(date(2026, 8, 26), date(2026, 8, 26))
        assert exc_info.value.rate_limited is True

    def test_raises_on_malformed_response_shape(self, httpx_mock):
        httpx_mock.add_response(url=CALENDAR_URL_PATTERN, json={"unexpected": "shape"})
        provider = EarningsApiCalendarProvider(api_key="test-key")

        with pytest.raises(EarningsApiError):
            provider.get_earnings_calendar(date(2026, 8, 26), date(2026, 8, 26))

    def test_empty_calendar_day_returns_empty_list_not_an_error(self, httpx_mock):
        """A real day with genuinely zero earnings (e.g. a weekend --
        confirmed live for a real Saturday) is an honest empty result,
        never an error."""
        httpx_mock.add_response(
            url=CALENDAR_URL_PATTERN,
            json={"date": "2026-08-22", "pre": [], "after": [], "notSupplied": []},
        )
        provider = EarningsApiCalendarProvider(api_key="test-key")

        entries = provider.get_earnings_calendar(date(2026, 8, 22), date(2026, 8, 22))

        assert entries == []


class TestGetCompanyProfile:
    def test_parses_real_response_and_normalizes_country(self, httpx_mock):
        httpx_mock.add_response(url=PROFILE_URL_PATTERN, json=REAL_PROFILE_RESPONSE)
        provider = EarningsApiCalendarProvider(api_key="test-key")

        profile = provider.get_company_profile("NVDA")

        assert profile is not None
        assert profile.symbol == "NVDA"
        assert profile.name == "NVIDIA CORP"
        assert profile.exchange == "NASDAQ"
        # "United States" (EarningsAPI's real field) -> "US" (this
        # project's eligibility check expects the ISO-2 code).
        assert profile.country == "US"
        # 5,049,594,080,000 raw dollars -> millions, same unit
        # convention FinnhubCompanyProfile.market_cap_millions already
        # uses.
        assert profile.market_cap_millions == Decimal("5049594.08")
        assert profile.source_provider == "earningsapi"

    def test_non_us_country_normalizes_to_none_not_the_raw_name(self, httpx_mock):
        """Regression test: earnings_calendar_event.country is a real
        String(4) column (sized for a 2-letter ISO code) -- confirmed
        live that EarningsAPI.com returns full country names for non-US
        symbols too ("Canada", "Argentina", "Denmark"), which raised a
        real StringDataRightTruncation error before this was fixed to
        normalize to None instead of passing the raw name through."""
        httpx_mock.add_response(
            url=PROFILE_URL_PATTERN,
            json={**REAL_PROFILE_RESPONSE, "symbol": "BNS", "country": "Canada"},
        )
        provider = EarningsApiCalendarProvider(api_key="test-key")

        profile = provider.get_company_profile("BNS")

        assert profile is not None
        assert profile.country is None

    def test_returns_none_for_unknown_symbol_404(self, httpx_mock):
        httpx_mock.add_response(url=PROFILE_URL_PATTERN, status_code=404)
        provider = EarningsApiCalendarProvider(api_key="test-key")

        assert provider.get_company_profile("ZZNOTREAL") is None

    def test_raises_on_invalid_api_key(self, httpx_mock):
        httpx_mock.add_response(url=PROFILE_URL_PATTERN, status_code=401)
        provider = EarningsApiCalendarProvider(api_key="bad-key")

        with pytest.raises(EarningsApiError):
            provider.get_company_profile("NVDA")
