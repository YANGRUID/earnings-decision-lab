"""A failed calendar sync retries today when -- and only when -- that could help.

Live evidence this is built against: the 2026-09-15 20:58 ET scheduled sync
died on ``[Errno -2] Name or service not known`` seconds after the Mac woke,
and the calendar then went 23 hours without a refresh, through a 15:30 ET
decision window, because nothing tried again. The opposite failure is equally
real: from 2026-09-10 the primary's plan allowance was spent, and retrying that
only spent the next period's allowance to re-learn the same refusal.
"""

import httpx
import pytest

from providers.earningsapi import EarningsApiError
from providers.fallback import AllProvidersFailedError
from services.calendar_sync_retry import (
    MAX_SYNC_ATTEMPTS,
    is_transient_sync_failure,
    retry_delay_for,
)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.earningsapi.com/v1/calendar/earnings")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request)
    )


class TestWhatCountsAsTransient:
    def test_the_real_2026_09_15_dns_failure_is_transient(self):
        assert is_transient_sync_failure(httpx.ConnectError("[Errno -2] Name or service not known"))

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ReadTimeout("timed out"),
            httpx.ConnectTimeout("timed out"),
            httpx.RemoteProtocolError("connection reset"),
        ],
    )
    def test_other_transport_failures_are_transient(self, exc):
        assert is_transient_sync_failure(exc)

    def test_a_provider_5xx_is_transient(self):
        assert is_transient_sync_failure(_http_error(503))

    def test_a_spent_plan_allowance_is_not(self):
        """The case that matters most: the provider answers a refusal fast and
        counts it, so retrying spends the next period's allowance to learn a
        fact this run already knows."""
        assert not is_transient_sync_failure(
            EarningsApiError(
                "free plan limit", rate_limited=True, quota_exhausted="FREE_QUOTA_EXCEEDED"
            )
        )

    def test_a_per_minute_rate_limit_is_transient(self):
        """Unlike the allowance above, this one clears on its own."""
        assert is_transient_sync_failure(EarningsApiError("slow down", rate_limited=True))

    @pytest.mark.parametrize("status", [401, 403, 404, 422])
    def test_settled_client_errors_are_not_transient(self, status):
        assert not is_transient_sync_failure(_http_error(status))

    def test_a_malformed_provider_response_is_not_transient(self):
        assert not is_transient_sync_failure(EarningsApiError("unexpected response shape"))

    def test_a_wrapped_transport_failure_is_still_seen(self):
        """The adapters chain the original with ``raise ... from exc``; the
        cause is read rather than the formatted message."""
        try:
            try:
                raise httpx.ConnectError("dns")
            except httpx.ConnectError as cause:
                raise EarningsApiError("EarningsAPI request failed") from cause
        except EarningsApiError as exc:
            assert is_transient_sync_failure(exc)


class TestAWholeChainFailure:
    def test_one_transient_provider_is_enough_to_retry(self):
        """The chain only needs one provider to answer, and it skips a spent
        one on its own -- so a spent primary beside a DNS-failed fallback is
        still worth another attempt."""
        exc = AllProvidersFailedError(
            [
                ("earningsapi", EarningsApiError("spent", quota_exhausted="FREE_QUOTA_EXCEEDED")),
                ("finnhub", httpx.ConnectError("[Errno -2] Name or service not known")),
            ]
        )
        assert is_transient_sync_failure(exc)

    def test_every_provider_settled_means_no_retry(self):
        exc = AllProvidersFailedError(
            [
                ("earningsapi", EarningsApiError("spent", quota_exhausted="FREE_QUOTA_EXCEEDED")),
                ("finnhub", _http_error(401)),
            ]
        )
        assert not is_transient_sync_failure(exc)


class TestTheRetryBudget:
    def test_it_is_bounded(self):
        """A provider that is genuinely down must surface as a failure in
        Operations, not disappear into an unbounded loop."""
        delays = [retry_delay_for(n) for n in range(1, MAX_SYNC_ATTEMPTS + 2)]
        assert delays[-1] is None
        assert delays[MAX_SYNC_ATTEMPTS - 1] is None
        assert all(d is not None for d in delays[: MAX_SYNC_ATTEMPTS - 1])

    def test_the_delays_widen(self):
        assert retry_delay_for(1) < retry_delay_for(2)

    def test_the_whole_budget_is_spent_well_before_a_decision_window(self):
        """A sync starting at 20:00 ET must be finished retrying the same
        night, never still trying at the next day's 15:30 window."""
        total = sum(
            (retry_delay_for(n) for n in range(1, MAX_SYNC_ATTEMPTS)),
            start=retry_delay_for(1) * 0,
        )
        assert total.total_seconds() < 3600
