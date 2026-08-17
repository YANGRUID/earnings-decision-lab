"""Real Alpha Vantage analyst-consensus adapter — two documented,
authenticated endpoints: EARNINGS_ESTIMATES (detailed consensus by fiscal
period) and EARNINGS_CALENDAR (the provider's own next-report-date
prediction). Both verified live against the real API during Phase 12
before this adapter was written — see docs/engineering_decisions.md.

Confirmed *not* premium-gated on this project's current Alpha Vantage plan,
unlike REALTIME_OPTIONS/HISTORICAL_OPTIONS (see providers/alpha_vantage_options.py).
"""

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from observability.http_client import new_http_client
from providers.alpha_vantage import AlphaVantageError
from providers.base import EarningsEstimatesProvider
from providers.types import EarningsEstimatePeriod, UpcomingEarningsCalendarEntry

_QUERY_URL = "https://www.alphavantage.co/query"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _decimal_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _int_or_none(value) -> int | None:
    d = _decimal_or_none(value)
    return int(d) if d is not None else None


class AlphaVantageEarningsEstimatesProvider(EarningsEstimatesProvider):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise ValueError("Alpha Vantage requires an API key (ALPHA_VANTAGE_API_KEY in .env)")
        self._api_key = api_key
        self._client = client or new_http_client(timeout=15.0)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def get_earnings_estimates(self, ticker: str) -> list[EarningsEstimatePeriod]:
        response = self._client.get(
            _QUERY_URL,
            params={
                "function": "EARNINGS_ESTIMATES",
                "symbol": ticker.upper(),
                "apikey": self._api_key,
            },
        )
        response.raise_for_status()
        payload = response.json()
        estimates = payload.get("estimates")
        if estimates is None:
            note = payload.get("Note") or payload.get("Information") or payload.get("Error Message")
            raise AlphaVantageError(note or f"unexpected response shape: {list(payload)}")

        retrieved_at = datetime.now(UTC)
        periods: list[EarningsEstimatePeriod] = []
        for entry in estimates:
            try:
                period_end = date.fromisoformat(entry["date"])
            except (KeyError, ValueError):
                continue
            periods.append(
                EarningsEstimatePeriod(
                    ticker=ticker.upper(),
                    fiscal_period_end_date=period_end,
                    horizon=entry.get("horizon", "unknown"),
                    eps_estimate_average=_decimal_or_none(entry.get("eps_estimate_average")),
                    eps_estimate_high=_decimal_or_none(entry.get("eps_estimate_high")),
                    eps_estimate_low=_decimal_or_none(entry.get("eps_estimate_low")),
                    eps_estimate_analyst_count=_int_or_none(entry.get("eps_estimate_analyst_count")),
                    eps_estimate_revision_up_30d=_int_or_none(
                        entry.get("eps_estimate_revision_up_trailing_30_days")
                    ),
                    eps_estimate_revision_down_30d=_int_or_none(
                        entry.get("eps_estimate_revision_down_trailing_30_days")
                    ),
                    revenue_estimate_average=_decimal_or_none(entry.get("revenue_estimate_average")),
                    revenue_estimate_high=_decimal_or_none(entry.get("revenue_estimate_high")),
                    revenue_estimate_low=_decimal_or_none(entry.get("revenue_estimate_low")),
                    revenue_estimate_analyst_count=_int_or_none(
                        entry.get("revenue_estimate_analyst_count")
                    ),
                    source_provider="alpha_vantage",
                    retrieved_at=retrieved_at,
                )
            )
        return periods

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def get_next_earnings_date(self, ticker: str) -> UpcomingEarningsCalendarEntry | None:
        response = self._client.get(
            _QUERY_URL,
            params={
                "function": "EARNINGS_CALENDAR",
                "symbol": ticker.upper(),
                "horizon": "3month",
                "apikey": self._api_key,
            },
        )
        response.raise_for_status()
        # EARNINGS_CALENDAR is the one Alpha Vantage endpoint used by this
        # project that returns CSV, not JSON, on success -- but it still
        # returns a JSON error body (like every other endpoint) on failure
        # (a wrong/rate-limited call), so a plain CSV parse would silently
        # treat an error message's text as a malformed row. Detected two
        # ways: the response Content-Type, and (belt-and-braces, since a
        # rate-limit response was observed live during Phase 12 development
        # without a JSON content-type) validating the actual CSV header
        # against the real schema before trusting any data row.
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            payload = response.json()
            note = (
                payload.get("Note") or payload.get("Information") or payload.get("Error Message")
            )
            raise AlphaVantageError(
                note or f"unexpected JSON response for EARNINGS_CALENDAR: {payload}"
            )

        expected_header = {
            "symbol",
            "name",
            "reportDate",
            "fiscalDateEnding",
            "estimate",
            "currency",
        }
        reader = csv.DictReader(io.StringIO(response.text))
        if reader.fieldnames is None or not expected_header.issubset(set(reader.fieldnames)):
            raise AlphaVantageError(
                "unexpected EARNINGS_CALENDAR response (not the expected CSV header), "
                f"first 300 chars: {response.text[:300]!r}"
            )
        rows = list(reader)
        if not rows:
            return None
        # horizon=3month can return multiple upcoming rows for a ticker in
        # rare cases (e.g. an estimate revision published mid-quarter) --
        # the nearest reportDate is the one this project treats as "next".
        row = min(rows, key=lambda r: r["reportDate"])
        try:
            report_date = date.fromisoformat(row["reportDate"])
            period_end = date.fromisoformat(row["fiscalDateEnding"])
        except (KeyError, ValueError) as exc:
            raise AlphaVantageError(f"unexpected EARNINGS_CALENDAR row shape: {row}") from exc

        return UpcomingEarningsCalendarEntry(
            ticker=ticker.upper(),
            fiscal_period_end_date=period_end,
            estimated_report_date=report_date,
            calendar_eps_estimate=_decimal_or_none(row.get("estimate")),
            source_provider="alpha_vantage",
            retrieved_at=datetime.now(UTC),
        )
