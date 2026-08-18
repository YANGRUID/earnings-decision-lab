import re
from datetime import date
from decimal import Decimal

import pytest

from providers.alpha_vantage import AlphaVantageError
from providers.alpha_vantage_estimates import AlphaVantageEarningsEstimatesProvider

URL_PATTERN = re.compile(r"https://www\.alphavantage\.co/query.*")

# Real-shaped EARNINGS_ESTIMATES response per Alpha Vantage's documented
# schema, captured during Phase 12 development.
REAL_ESTIMATES_RESPONSE = {
    "symbol": "MU",
    "estimates": [
        {
            "date": "2026-08-31",
            "horizon": "current quarter",
            "eps_estimate_average": "2.85",
            "eps_estimate_high": "3.20",
            "eps_estimate_low": "2.50",
            "eps_estimate_analyst_count": "24",
            "eps_estimate_revision_up_trailing_30_days": "12",
            "eps_estimate_revision_down_trailing_30_days": "3",
            "revenue_estimate_average": "11200000000",
            "revenue_estimate_high": "11800000000",
            "revenue_estimate_low": "10600000000",
            "revenue_estimate_analyst_count": "20",
        },
        {
            "date": "not-a-real-date",
            "horizon": "current year",
            "eps_estimate_average": "12.00",
        },
    ],
}

# Real EARNINGS_CALENDAR CSV shape (horizon=3month) -- success path.
REAL_CALENDAR_CSV = (
    "symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\n"
    "MU,Micron Technology Inc,2026-09-24,2026-08-31,2.85,USD\r\n"
)

REAL_CALENDAR_CSV_MULTIPLE_ROWS = (
    "symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\n"
    "MU,Micron Technology Inc,2026-12-17,2026-11-30,3.10,USD\r\n"
    "MU,Micron Technology Inc,2026-09-24,2026-08-31,2.85,USD\r\n"
)

REAL_CALENDAR_CSV_EMPTY = "symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\n"

# The real rate-limit-induced malformed response observed live during Phase
# 12 development: "Information" spelled character-by-character across the
# expected CSV columns, no JSON content-type. Must never be parsed as a
# real row.
MALFORMED_RATE_LIMIT_CSV = (
    "symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\nI,n,f,o,r,m\r\n"
)

# The exact 7-column live payload observed during Phase 14.9 AAPL root-cause
# investigation (this project's Alpha Vantage key at its 25-requests/day
# cap) -- confirmed via a direct live call, not synthesized. This is the
# actual root cause of the real AAPL "no next earnings date" / "no analyst
# consensus" preparation-step failure the user reported: EARNINGS_CALENDAR
# degrades to this malformed CSV row instead of the JSON error body every
# other Alpha Vantage endpoint returns under the same rate limit.
LIVE_RATE_LIMIT_CSV = (
    "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\r\n"
    "I,n,f,o,r,m,a\r\n"
)


def test_requires_api_key():
    with pytest.raises(ValueError):
        AlphaVantageEarningsEstimatesProvider(api_key="")


def test_get_earnings_estimates_parses_real_response(httpx_mock):
    httpx_mock.add_response(url=URL_PATTERN, json=REAL_ESTIMATES_RESPONSE)
    provider = AlphaVantageEarningsEstimatesProvider(api_key="test-key")

    periods = provider.get_earnings_estimates("MU")

    assert len(periods) == 1  # the malformed "not-a-real-date" entry is skipped
    period = periods[0]
    assert period.fiscal_period_end_date == date(2026, 8, 31)
    assert period.horizon == "current quarter"
    assert period.eps_estimate_average == Decimal("2.85")
    assert period.eps_estimate_analyst_count == 24
    assert period.eps_estimate_revision_up_30d == 12
    assert period.eps_estimate_revision_down_30d == 3
    assert period.revenue_estimate_average == Decimal("11200000000")
    assert period.source_provider == "alpha_vantage"


def test_get_earnings_estimates_raises_on_rate_limit_note(httpx_mock):
    httpx_mock.add_response(
        url=URL_PATTERN,
        json={"Note": "Thank you for using Alpha Vantage! Rate limit reached."},
    )
    provider = AlphaVantageEarningsEstimatesProvider(api_key="test-key")

    with pytest.raises(AlphaVantageError) as exc_info:
        provider.get_earnings_estimates("MU")
    assert exc_info.value.rate_limited is True


def test_get_next_earnings_date_parses_real_csv(httpx_mock):
    httpx_mock.add_response(
        url=URL_PATTERN, text=REAL_CALENDAR_CSV, headers={"content-type": "application/x-download"}
    )
    provider = AlphaVantageEarningsEstimatesProvider(api_key="test-key")

    entry = provider.get_next_earnings_date("MU")

    assert entry is not None
    assert entry.fiscal_period_end_date == date(2026, 8, 31)
    assert entry.estimated_report_date == date(2026, 9, 24)
    assert entry.calendar_eps_estimate == Decimal("2.85")
    assert entry.source_provider == "alpha_vantage"


def test_get_next_earnings_date_picks_nearest_report_date_among_multiple_rows(httpx_mock):
    httpx_mock.add_response(
        url=URL_PATTERN,
        text=REAL_CALENDAR_CSV_MULTIPLE_ROWS,
        headers={"content-type": "application/x-download"},
    )
    provider = AlphaVantageEarningsEstimatesProvider(api_key="test-key")

    entry = provider.get_next_earnings_date("MU")

    assert entry is not None
    assert entry.estimated_report_date == date(2026, 9, 24)


def test_get_next_earnings_date_returns_none_for_empty_csv(httpx_mock):
    httpx_mock.add_response(
        url=URL_PATTERN,
        text=REAL_CALENDAR_CSV_EMPTY,
        headers={"content-type": "application/x-download"},
    )
    provider = AlphaVantageEarningsEstimatesProvider(api_key="test-key")

    assert provider.get_next_earnings_date("MU") is None


def test_get_next_earnings_date_raises_on_json_error_response(httpx_mock):
    httpx_mock.add_response(
        url=URL_PATTERN,
        json={"Note": "Thank you for using Alpha Vantage! Rate limit reached."},
    )
    provider = AlphaVantageEarningsEstimatesProvider(api_key="test-key")

    with pytest.raises(AlphaVantageError) as exc_info:
        provider.get_next_earnings_date("MU")
    assert exc_info.value.rate_limited is True


def test_get_next_earnings_date_raises_on_malformed_rate_limit_csv(httpx_mock):
    httpx_mock.add_response(
        url=URL_PATTERN,
        text=MALFORMED_RATE_LIMIT_CSV,
        headers={"content-type": "application/x-download"},
    )
    provider = AlphaVantageEarningsEstimatesProvider(api_key="test-key")

    with pytest.raises(AlphaVantageError) as exc_info:
        provider.get_next_earnings_date("MU")
    assert exc_info.value.rate_limited is True


def test_get_next_earnings_date_raises_rate_limited_on_live_observed_payload(httpx_mock):
    """Regression test for the confirmed real AAPL root cause (Phase 14.9):
    reproduces, byte-for-byte, the malformed CSV this project's Alpha
    Vantage key actually returned live for EARNINGS_CALENDAR once its
    25-requests/day quota was exhausted -- a syntactically valid header
    followed by one data row whose single-character values spell
    "Informa[tion]". Before this phase, this fell through to the generic
    `except (KeyError, ValueError)` date-parsing branch and was reported as
    an opaque parse failure; it must now be caught earlier and classified
    as a rate limit, not a generic malformed-row error."""
    httpx_mock.add_response(
        url=URL_PATTERN,
        text=LIVE_RATE_LIMIT_CSV,
        headers={"content-type": "application/x-download"},
    )
    provider = AlphaVantageEarningsEstimatesProvider(api_key="test-key")

    with pytest.raises(AlphaVantageError) as exc_info:
        provider.get_next_earnings_date("AAPL")
    assert exc_info.value.rate_limited is True
    assert "rate-limit-degraded" in str(exc_info.value)


def test_alpha_vantage_error_defaults_to_not_rate_limited():
    assert AlphaVantageError("some other failure").rate_limited is False
