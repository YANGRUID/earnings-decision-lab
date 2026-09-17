"""Usage rows must carry what a calendar provider actually said. From
2026-09-10 every EarningsAPI refusal was recorded with no status and
rate_limited=False, so the spent allowance was invisible in the data."""

from datetime import date

from providers.earningsapi import EarningsApiError
from providers.finnhub import FinnhubError
from services.usage_instrumentation import _classify_exception, _request_units


def test_a_spent_allowance_is_recorded_by_its_code():
    exc = EarningsApiError("429", rate_limited=True, quota_exhausted="FREE_QUOTA_EXCEEDED")
    assert _classify_exception(exc) == ("FREE_QUOTA_EXCEEDED", True)


def test_a_rate_limit_is_recorded_as_one():
    assert _classify_exception(EarningsApiError("429", rate_limited=True)) == ("429", True)
    assert _classify_exception(FinnhubError("401", rate_limited=False)) == ("error", False)


def test_a_calendar_range_counts_one_request_per_date():
    assert _request_units("get_earnings_calendar", (date(2026, 9, 17), date(2026, 9, 24))) == 8
    assert _request_units("get_earnings_calendar", (date(2026, 9, 17), date(2026, 9, 17))) == 1
    assert _request_units("get_company_profile", ("NVDA",)) is None
