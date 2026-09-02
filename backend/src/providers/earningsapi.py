"""Real EarningsAPI.com adapter -- the earnings calendar's PRIMARY source
as of this phase (see EARNINGS_CALENDAR_PROVIDER_ARCHITECTURE_REVIEW.md).
Finnhub (providers/finnhub.py) is now the FALLBACK: its free tier was
confirmed live, against this project's own real data, to return
far-future placeholder dates (clustering around May-June 2027, even for
mega-caps with well-known real quarterly cadences) instead of erroring
once its own near-term coverage is exhausted -- so the sync never
failed, it just silently stored dates that didn't reflect reality.
EarningsAPI.com was confirmed live (2026-08-22, real key) to return
genuinely near-term real dates instead (e.g. NVDA's real 2026-08-26
report, 4 days out at verification time).

Same established provider-adapter shape as providers/finnhub.py: httpx +
tenacity, no vendor SDK, Pydantic boundary types (providers/types.py)
that never let a malformed response reach a caller silently.

Real, confirmed-live API shape (no official published OpenAPI spec to
generate a client from -- verified by hand against real responses, the
same discipline this project used for IBKR's own undocumented quirks):

    GET /v1/calendar/earnings?date=YYYY-MM-DD&apikey=...
        -> {"date": "...", "pre": [...], "after": [...], "notSupplied": [...]}
        One calendar DATE per call, never a range -- unlike Finnhub's own
        from/to range endpoint. Each entry: symbol, name, epsEstimate,
        eps (actual, unused here), revenue (actual, unused here),
        revenueEstimate. Real revenue estimates ARE present -- a genuine
        improvement over some alternative sources evaluated during this
        same review that had no revenue field at all.
    GET /v1/profile/{symbol}?apikey=...
        -> {symbol, companyName, exchange, country, marketCap, sector,
            industry, cik, type, tags, ...}
        ``country`` is a full name ("United States"), NOT the ISO-2 "US"
        this codebase's eligibility check compares against
        (services/earnings_eligibility.py::US_COUNTRY_CODE) -- mapped
        explicitly below, never assumed to already match.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from observability.http_client import new_http_client
from providers.base import EarningsCalendarProvider
from providers.types import FinnhubCalendarEntry, FinnhubCompanyProfile

_BASE_URL = "https://api.earningsapi.com/v1"

# Real, confirmed-live mapping (EarningsAPI's own "country" field is a
# full display name -- "Canada", "Argentina", "Denmark", etc -- not an
# ISO code). earnings_calendar_event.country is `String(4)` (sized for a
# 2-letter code, see models/earnings_calendar_event.py), and this
# project's eligibility check (services/earnings_eligibility.py) only
# ever compares against "US" -- so only the one real value it needs is
# mapped; every other real country name is intentionally normalized to
# None rather than passed through raw, which would violate that column's
# real length constraint (confirmed live: "Canada"/"Argentina"/"Denmark"
# all raised StringDataRightTruncation before this fix). A None country
# is an honest "not confidently mapped to the one code this project
# checks," not data loss -- nothing downstream reads country for a
# non-US comparison.
_COUNTRY_NAME_TO_ISO = {"united states": "US"}


class EarningsApiError(Exception):
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


def _normalize_country(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _COUNTRY_NAME_TO_ISO.get(raw.strip().lower())


class EarningsApiCalendarProvider(EarningsCalendarProvider):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise ValueError("EarningsAPI.com requires an API key (EARNINGSAPI_API_KEY in .env)")
        self._api_key = api_key
        self._client = client or new_http_client(timeout=15.0)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        """The one real HTTP call, retried on transport errors/429/5xx --
        matches providers/finnhub.py::FinnhubEarningsCalendarProvider._get
        exactly, including why: callers convert the *final* httpx
        exception into a clean EarningsApiError themselves, so tenacity
        always sees the raw exception it needs to match against."""
        response = self._client.get(
            f"{_BASE_URL}{path}", params={**params, "apikey": self._api_key}
        )
        response.raise_for_status()
        return response

    def _fetch_one_day(self, day: date) -> list[FinnhubCalendarEntry]:
        """One real calendar date -- EarningsAPI.com has no range
        endpoint (confirmed live), unlike Finnhub's own from/to call.
        get_earnings_calendar() below loops this per real calendar day
        actually needed, never every day in a wide range regardless of
        whether it's already covered -- see services/earnings_calendar_
        sync.py's own per-date dedup for why this stays cheap."""
        try:
            response = self._get("/calendar/earnings", {"date": day.isoformat()})
        except httpx.HTTPStatusError as exc:
            raise EarningsApiError(
                f"EarningsAPI /calendar/earnings request failed ({exc.response.status_code}) "
                f"for {day.isoformat()}: {exc.response.text[:200]!r}",
                rate_limited=exc.response.status_code == 429,
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict) or "pre" not in payload:
            raise EarningsApiError(
                f"unexpected /calendar/earnings response shape for {day.isoformat()}: "
                f"{payload if not isinstance(payload, dict) else list(payload)}"
            )

        retrieved_at = datetime.now(UTC)
        entries: list[FinnhubCalendarEntry] = []
        # Timing is which real bucket an entry is in, not a field on the
        # entry itself -- confirmed live; matches this project's own
        # session convention (bmo/amc/""), see _map_timing in services/
        # earnings_calendar_sync.py, which "notSupplied" maps to UNKNOWN
        # via the same empty-string convention Finnhub's own "" already
        # uses there.
        for bucket, session in (("pre", "bmo"), ("after", "amc"), ("notSupplied", "")):
            for raw in payload.get(bucket) or []:
                symbol = raw.get("symbol")
                if not symbol:
                    continue
                entries.append(
                    FinnhubCalendarEntry(
                        symbol=symbol.upper(),
                        earnings_date=day,
                        session=session,
                        fiscal_year=None,
                        fiscal_quarter=None,
                        eps_estimate=_decimal_or_none(raw.get("epsEstimate")),
                        revenue_estimate=_decimal_or_none(raw.get("revenueEstimate")),
                        source_provider="earningsapi",
                        retrieved_at=retrieved_at,
                    )
                )
        return entries

    def get_earnings_calendar(self, from_date: date, to_date: date) -> list[FinnhubCalendarEntry]:
        """Every real date in [from_date, to_date], inclusive -- one real
        HTTP call per date (see _fetch_one_day). A caller wanting to stay
        inside the free tier's real 100/day budget should pass a narrow
        range and rely on services/earnings_calendar_sync.py's per-date
        dedup rather than calling this directly with a wide range on
        every run -- this method itself has no memory of what was
        already fetched on a previous call; that's the sync service's
        job, not the provider's.
        """
        entries: list[FinnhubCalendarEntry] = []
        day = from_date
        while day <= to_date:
            entries.extend(self._fetch_one_day(day))
            day = date.fromordinal(day.toordinal() + 1)
        return entries

    def get_company_profile(self, symbol: str) -> FinnhubCompanyProfile | None:
        """Real name/exchange/country/market-cap for ``symbol``. Country
        is normalized from EarningsAPI's full display name to the ISO-2
        code this project's eligibility check expects (see module
        docstring) -- everything else is passed through as EarningsAPI
        actually returned it."""
        try:
            response = self._get(f"/profile/{symbol.upper()}", {})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise EarningsApiError(
                f"EarningsAPI /profile/{symbol.upper()} request failed "
                f"({exc.response.status_code}): {exc.response.text[:200]!r}",
                rate_limited=exc.response.status_code == 429,
            ) from exc
        payload = response.json()
        if not payload or not payload.get("companyName"):
            return None

        market_cap_dollars = _decimal_or_none(payload.get("marketCap"))
        return FinnhubCompanyProfile(
            symbol=symbol.upper(),
            name=payload.get("companyName"),
            logo_url=None,  # EarningsAPI's profile endpoint doesn't provide one
            exchange=payload.get("exchange"),
            country=_normalize_country(payload.get("country")),
            market_cap_millions=(
                market_cap_dollars / Decimal(1_000_000) if market_cap_dollars is not None else None
            ),
            currency=None,  # not provided by this endpoint
            source_provider="earningsapi",
            retrieved_at=datetime.now(UTC),
        )
