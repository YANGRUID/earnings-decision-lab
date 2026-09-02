"""Real, minimal, read-only "Test Connection" checks -- one per real
provider adapter. Every check does the cheapest real call that actually
proves connectivity+auth, never a fabricated "ok". Results are recorded as
a ProviderHealthEvent (see services/provider_status.py) so a past test
result stays visible even after this process restarts.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from core.config import Settings
from models.enums import ProviderHealthStatus
from providers.alpha_vantage import AlphaVantageError, AlphaVantageMarketDataProvider
from providers.alpha_vantage_estimates import AlphaVantageEarningsEstimatesProvider
from providers.alpha_vantage_options import (
    AlphaVantageOptionsProvider,
    PremiumEndpointRequiredError,
)
from providers.earningsapi import EarningsApiCalendarProvider
from providers.ibkr_client import (
    IBKRClient,
    IBKRCompetingSessionError,
    IBKRGatewayUnavailableError,
    IBKRNotAuthenticatedError,
    IBKRRateLimitedError,
)
from providers.ibkr_tws_health import TwsHealthProbe
from providers.sec_edgar import SECEdgarProvider
from providers.tiingo import TiingoMarketDataProvider
from services.llm.errors import LLMError, MissingAPIKeyError, UnknownProviderError
from services.llm.factory import get_llm_provider
from services.llm.types import ChatMessage
from services.secret_store import resolve_secret
from services.system_status import get_tws_status

# A real, always-listed, large-cap ticker -- cheap and reliable to probe
# with, never the research subject of the test itself.
_PROBE_TICKER = "AAPL"
_PROBE_CIK = "0000320193"  # AAPL, a real, stable CIK for a lightweight EDGAR check


class UnknownTestConnectionTargetError(Exception):
    pass


def _map_http_status(exc: httpx.HTTPStatusError) -> tuple[ProviderHealthStatus, str]:
    code = exc.response.status_code
    if code in (401, 403):
        return ProviderHealthStatus.AUTH_FAILED, f"HTTP {code}"
    if code == 429:
        return ProviderHealthStatus.RATE_LIMITED, f"HTTP {code}"
    if code >= 500:
        return ProviderHealthStatus.UNAVAILABLE, f"HTTP {code}"
    return ProviderHealthStatus.UNAVAILABLE, f"HTTP {code}"


def _test_tiingo(settings: Settings, db: Session | None) -> tuple[ProviderHealthStatus, str | None]:
    key = resolve_secret(settings, "tiingo", db)
    if not key:
        return ProviderHealthStatus.AUTH_FAILED, "TIINGO_API_KEY not configured"
    provider = TiingoMarketDataProvider(api_key=key)
    try:
        end = date.today()
        provider.get_daily_bars(_PROBE_TICKER, end - timedelta(days=5), end)
        return ProviderHealthStatus.CONNECTED, None
    except httpx.HTTPStatusError as exc:
        return _map_http_status(exc)
    except httpx.TransportError as exc:
        return ProviderHealthStatus.UNAVAILABLE, str(exc)


def _test_alpha_vantage_prices(
    settings: Settings, db: Session | None
) -> tuple[ProviderHealthStatus, str | None]:
    key = resolve_secret(settings, "alpha_vantage", db)
    if not key:
        return ProviderHealthStatus.AUTH_FAILED, "ALPHA_VANTAGE_API_KEY not configured"
    provider = AlphaVantageMarketDataProvider(api_key=key)
    try:
        end = date.today()
        provider.get_daily_bars(_PROBE_TICKER, end - timedelta(days=5), end)
        return ProviderHealthStatus.CONNECTED, None
    except AlphaVantageError as exc:
        text = str(exc).lower()
        if "premium" in text:
            return ProviderHealthStatus.PREMIUM_REQUIRED, str(exc)
        if "frequency" in text or "rate limit" in text or "per minute" in text:
            return ProviderHealthStatus.RATE_LIMITED, str(exc)
        return ProviderHealthStatus.UNAVAILABLE, str(exc)
    except httpx.HTTPStatusError as exc:
        return _map_http_status(exc)
    except httpx.TransportError as exc:
        return ProviderHealthStatus.UNAVAILABLE, str(exc)


def _test_alpha_vantage_estimates(
    settings: Settings, db: Session | None
) -> tuple[ProviderHealthStatus, str | None]:
    key = resolve_secret(settings, "alpha_vantage", db)
    if not key:
        return ProviderHealthStatus.AUTH_FAILED, "ALPHA_VANTAGE_API_KEY not configured"
    provider = AlphaVantageEarningsEstimatesProvider(api_key=key)
    try:
        provider.get_next_earnings_date(_PROBE_TICKER)
        return ProviderHealthStatus.CONNECTED, None
    except AlphaVantageError as exc:
        text = str(exc).lower()
        if "premium" in text:
            return ProviderHealthStatus.PREMIUM_REQUIRED, str(exc)
        if "frequency" in text or "rate limit" in text or "per minute" in text:
            return ProviderHealthStatus.RATE_LIMITED, str(exc)
        return ProviderHealthStatus.UNAVAILABLE, str(exc)
    except httpx.HTTPStatusError as exc:
        return _map_http_status(exc)
    except httpx.TransportError as exc:
        return ProviderHealthStatus.UNAVAILABLE, str(exc)


def _test_alpha_vantage_options(
    settings: Settings, db: Session | None
) -> tuple[ProviderHealthStatus, str | None]:
    key = resolve_secret(settings, "alpha_vantage", db)
    if not key:
        return ProviderHealthStatus.AUTH_FAILED, "ALPHA_VANTAGE_API_KEY not configured"
    provider = AlphaVantageOptionsProvider(api_key=key)
    try:
        provider.get_option_chain(_PROBE_TICKER, datetime.now(UTC))
        return ProviderHealthStatus.CONNECTED, None
    except PremiumEndpointRequiredError as exc:
        return ProviderHealthStatus.PREMIUM_REQUIRED, str(exc)
    except AlphaVantageError as exc:
        text = str(exc).lower()
        if "frequency" in text or "rate limit" in text or "per minute" in text:
            return ProviderHealthStatus.RATE_LIMITED, str(exc)
        return ProviderHealthStatus.UNAVAILABLE, str(exc)
    except httpx.HTTPStatusError as exc:
        return _map_http_status(exc)
    except httpx.TransportError as exc:
        return ProviderHealthStatus.UNAVAILABLE, str(exc)


def _test_sec_edgar(settings: Settings) -> tuple[ProviderHealthStatus, str | None]:
    provider = SECEdgarProvider(user_agent=settings.sec_edgar_user_agent)
    try:
        provider.get_company_facts(_PROBE_CIK)
        return ProviderHealthStatus.CONNECTED, None
    except httpx.HTTPStatusError as exc:
        return _map_http_status(exc)
    except httpx.TransportError as exc:
        return ProviderHealthStatus.UNAVAILABLE, str(exc)


def _test_ibkr(
    settings: Settings, tws_probe: TwsHealthProbe | None
) -> tuple[ProviderHealthStatus, str | None]:
    """IBKR TWS Migration, Phase 3 readiness (Section 31/42) -- a real gap
    this task's frontend audit surfaced: this always tested the Web
    Gateway, even when ``ibkr_provider=tws`` is configured, so the Data
    Providers page's generic "Test Connection" button silently tested the
    wrong transport instead of the one actually serving requests.
    Delegates to the TWS branch below for that case; the Web check below
    is completely unchanged for ``ibkr_provider=web`` (the default)."""
    if settings.ibkr_provider.lower() == "tws":
        return _test_ibkr_tws(settings, tws_probe)
    client = IBKRClient(base_url=settings.ibkr_base_url)
    try:
        client.ensure_authenticated()
        return ProviderHealthStatus.CONNECTED, None
    except IBKRGatewayUnavailableError as exc:
        return ProviderHealthStatus.GATEWAY_OFFLINE, str(exc)
    except IBKRNotAuthenticatedError as exc:
        return ProviderHealthStatus.AUTH_FAILED, str(exc)
    except IBKRCompetingSessionError as exc:
        return ProviderHealthStatus.UNAVAILABLE, str(exc)
    except IBKRRateLimitedError as exc:
        return ProviderHealthStatus.RATE_LIMITED, str(exc)


def _test_ibkr_tws(
    settings: Settings, tws_probe: TwsHealthProbe | None
) -> tuple[ProviderHealthStatus, str | None]:
    """Real, live-discovered bug (production cutover, 2026-09-01): this
    used to always call get_tws_status(settings, probe=None), which
    spawns its own one-shot TWSConnectionManager at client_id =
    ibkr_tws_client_id + HEALTHCHECK_CLIENT_ID_OFFSET (see that
    function's own docstring) -- the EXACT same client id the app's own
    persistent app.state.tws_health_probe already holds open. Calling
    this from a running app (which always has one) collided with it
    live: a real IB Gateway error 326 ("client id already in use"),
    confirmed live against the real Gateway. Reusing the app's own probe
    here (passed through from the router) is both correct AND cheaper --
    a plain attribute read instead of a fresh connect/disconnect. Falls
    back to get_tws_status's own probe=None one-shot behavior only for a
    caller with no persistent probe of its own (e.g. a direct service-
    layer test)."""
    status = get_tws_status(settings, probe=tws_probe)
    if status.status_label == "CONNECTED":
        return ProviderHealthStatus.CONNECTED, None
    if status.status_label == "GATEWAY_UNREACHABLE":
        return ProviderHealthStatus.GATEWAY_OFFLINE, status.error
    if status.status_label == "AUTH_REQUIRED":
        return ProviderHealthStatus.AUTH_FAILED, status.error
    return ProviderHealthStatus.UNAVAILABLE, status.error


def _test_llm(
    settings: Settings, provider_name: str, db: Session | None
) -> tuple[ProviderHealthStatus, str | None]:
    try:
        provider = get_llm_provider(settings, override_provider=provider_name, db=db)
    except MissingAPIKeyError as exc:
        return ProviderHealthStatus.AUTH_FAILED, str(exc)
    except UnknownProviderError as exc:
        return ProviderHealthStatus.UNAVAILABLE, str(exc)
    try:
        # Smallest real completion that still proves auth+model+connectivity
        # -- a couple of tokens, not a real research call.
        provider.generate(
            [ChatMessage(role="user", content="Reply with the single word: OK")],
            max_tokens=5,
        )
        return ProviderHealthStatus.CONNECTED, None
    except LLMError as exc:
        text = str(exc).lower()
        if "401" in text or "unauthorized" in text or "invalid" in text and "key" in text:
            return ProviderHealthStatus.AUTH_FAILED, str(exc)
        if "429" in text or "rate limit" in text:
            return ProviderHealthStatus.RATE_LIMITED, str(exc)
        return ProviderHealthStatus.UNAVAILABLE, str(exc)
    except httpx.HTTPStatusError as exc:
        return _map_http_status(exc)
    except httpx.TransportError as exc:
        return ProviderHealthStatus.UNAVAILABLE, str(exc)


def _test_earningsapi(
    settings: Settings, db: Session | None
) -> tuple[ProviderHealthStatus, str | None]:
    """Earnings-calendar primary provider (added 2026-09-02).

    Until now no test-connection check existed for this provider, so the
    Settings "Test Connection" button reported an error even while the
    scheduled calendar sync was succeeding with the same key -- which is
    exactly what happened when the key was rotated. One real, authenticated,
    single-day calendar request: the cheapest call the API offers.
    """
    key = None
    try:
        key = resolve_secret(settings, "earningsapi", db)
    except Exception:  # noqa: BLE001 -- unknown store key falls through to the env var
        key = None
    key = key or getattr(settings, "earningsapi_api_key", None) or ""
    if not key:
        return ProviderHealthStatus.AUTH_FAILED, "EARNINGSAPI_API_KEY not configured"
    provider = EarningsApiCalendarProvider(api_key=key)
    try:
        today = date.today()
        provider.get_earnings_calendar(today, today)
        return ProviderHealthStatus.CONNECTED, None
    except httpx.HTTPStatusError as exc:
        return _map_http_status(exc)
    except httpx.TransportError as exc:
        return ProviderHealthStatus.UNAVAILABLE, str(exc)
    except Exception as exc:  # noqa: BLE001 -- provider wraps some errors in its own type
        text = str(exc)
        if "429" in text or "rate" in text.lower():
            return ProviderHealthStatus.RATE_LIMITED, text
        if "401" in text or "403" in text or "auth" in text.lower():
            return ProviderHealthStatus.AUTH_FAILED, text
        return ProviderHealthStatus.UNAVAILABLE, text


def test_connection(
    settings: Settings,
    provider: str,
    domain: str,
    db: Session | None = None,
    tws_probe: TwsHealthProbe | None = None,
) -> tuple[ProviderHealthStatus, str | None]:
    """Runs the real, minimal connectivity check for ``provider`` in
    ``domain``, against whichever key is actually live right now -- an
    owner-configured credential (see services/secret_store/) if one is
    stored, else the env var, exactly what a real research request would
    use. Raises UnknownTestConnectionTargetError for any (provider, domain)
    pair that isn't a real, wired-up adapter -- never silently returns a
    fabricated "connected". ``tws_probe`` (Section 31/42, cutover
    hardening) is the app's own persistent TwsHealthProbe when the caller
    has one -- see _test_ibkr_tws's own docstring for why passing it
    through, rather than defaulting to None, is what avoids a real client-
    id collision against that same persistent connection."""
    if domain == "price_history" and provider == "tiingo":
        return _test_tiingo(settings, db)
    if domain == "price_history" and provider == "alpha_vantage":
        return _test_alpha_vantage_prices(settings, db)
    if domain == "earnings_estimates" and provider == "alpha_vantage":
        return _test_alpha_vantage_estimates(settings, db)
    if domain == "filings" and provider == "sec_edgar":
        return _test_sec_edgar(settings)
    if domain == "earnings_calendar" and provider == "earningsapi":
        return _test_earningsapi(settings, db)
    if domain == "options" and provider == "ibkr":
        return _test_ibkr(settings, tws_probe)
    if domain == "options" and provider == "alpha_vantage":
        return _test_alpha_vantage_options(settings, db)
    if domain == "llm" and provider in ("deepseek", "openai", "anthropic", "openai_compatible"):
        return _test_llm(settings, provider, db)

    raise UnknownTestConnectionTargetError(
        f"no test-connection check exists for provider={provider!r} domain={domain!r}"
    )
