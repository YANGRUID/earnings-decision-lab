"""Real Finnhub adapter -- Phase 4's single source of truth for the
forward-looking, cross-symbol earnings calendar (`/calendar/earnings`)
and, for calendar entries that pass the eligibility filter (Phase 5),
real company reference data (`/stock/profile2`: name, logo, market cap).

Deliberately does not touch anything Alpha-Vantage-sourced -- the
existing per-ticker "next earnings date" flow
(services/market_expectations.py, providers/alpha_vantage_estimates.py)
is untouched by this file and keeps using Alpha Vantage, per
ARCHITECTURE_REVIEW_PHASE4.md section 0.1.

Follows this project's established provider-adapter shape exactly
(httpx + tenacity, no vendor SDK -- see providers/alpha_vantage_estimates.py):
one retryable-error predicate, one real API surface, Pydantic boundary
types (providers/types.py) that never let a malformed response reach a
caller silently.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from observability.http_client import new_http_client
from providers.base import EarningsCalendarProvider
from providers.types import FinnhubCalendarEntry, FinnhubCompanyProfile

_BASE_URL = "https://finnhub.io/api/v1"


class FinnhubError(Exception):
    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _int_or_none(value: object) -> int | None:
    d = _decimal_or_none(value)
    return int(d) if d is not None else None


class FinnhubEarningsCalendarProvider(EarningsCalendarProvider):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise ValueError("Finnhub requires an API key (FINNHUB_API_KEY in .env)")
        self._api_key = api_key
        self._client = client or new_http_client(timeout=15.0)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        """The one real HTTP call, retried on transport errors/429/5xx via
        ``_retryable`` -- callers convert the *final* httpx exception (the
        one that survives all retries) into a clean FinnhubError
        themselves, so tenacity always sees the raw httpx exception it
        needs to match against, never an already-wrapped one."""
        response = self._client.get(f"{_BASE_URL}{path}", params=params)
        response.raise_for_status()
        return response

    def get_earnings_calendar(
        self, from_date: date, to_date: date
    ) -> list[FinnhubCalendarEntry]:
        try:
            response = self._get(
                "/calendar/earnings",
                {
                    "from": from_date.isoformat(),
                    "to": to_date.isoformat(),
                    "token": self._api_key,
                },
            )
        except httpx.HTTPStatusError as exc:
            raise FinnhubError(
                f"Finnhub /calendar/earnings request failed ({exc.response.status_code}): "
                f"{exc.response.text[:200]!r}",
                rate_limited=exc.response.status_code == 429,
            ) from exc
        payload = response.json()
        raw_entries = payload.get("earningsCalendar")
        if raw_entries is None:
            raise FinnhubError(
                f"unexpected /calendar/earnings response shape: {list(payload)}"
            )

        retrieved_at = datetime.now(UTC)
        entries: list[FinnhubCalendarEntry] = []
        for raw in raw_entries:
            symbol = raw.get("symbol")
            raw_date = raw.get("date")
            if not symbol or not raw_date:
                continue
            try:
                earnings_date = date.fromisoformat(raw_date)
            except ValueError:
                continue
            entries.append(
                FinnhubCalendarEntry(
                    symbol=symbol.upper(),
                    earnings_date=earnings_date,
                    session=(raw.get("hour") or "").strip().lower(),
                    fiscal_year=_int_or_none(raw.get("year")),
                    fiscal_quarter=_int_or_none(raw.get("quarter")),
                    eps_estimate=_decimal_or_none(raw.get("epsEstimate")),
                    revenue_estimate=_decimal_or_none(raw.get("revenueEstimate")),
                    source_provider="finnhub",
                    retrieved_at=retrieved_at,
                )
            )
        return entries

    def get_company_profile(self, symbol: str) -> FinnhubCompanyProfile | None:
        """Real name/logo/market-cap/exchange/country for ``symbol`` --
        the calendar endpoint above carries none of these, only a raw
        symbol. Finnhub returns HTTP 200 with an empty JSON object (not a
        404) for an unknown/delisted symbol; treated as "no profile
        available", never as a malformed response."""
        try:
            response = self._get(
                "/stock/profile2", {"symbol": symbol.upper(), "token": self._api_key}
            )
        except httpx.HTTPStatusError as exc:
            raise FinnhubError(
                f"Finnhub /stock/profile2 request failed ({exc.response.status_code}): "
                f"{exc.response.text[:200]!r}",
                rate_limited=exc.response.status_code == 429,
            ) from exc
        payload = response.json()
        if not payload or not payload.get("name"):
            return None

        return FinnhubCompanyProfile(
            symbol=symbol.upper(),
            name=payload.get("name"),
            logo_url=payload.get("logo") or None,
            exchange=payload.get("exchange"),
            country=payload.get("country"),
            market_cap_millions=_decimal_or_none(payload.get("marketCapitalization")),
            currency=payload.get("currency"),
            source_provider="finnhub",
            retrieved_at=datetime.now(UTC),
        )
