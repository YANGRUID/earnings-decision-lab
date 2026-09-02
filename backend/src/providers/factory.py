"""Constructs configured providers from real app config plus an optional
owner override (see services/provider_settings.py). This is the only place
in the codebase that knows OPTIONS_PROVIDER/market-data-provider selection
exists -- everything else (ingestion scripts, services/options_analytics.py,
services/research_orchestration.py) depends on the provider interfaces,
never on a concrete provider. Mirrors services/llm/factory.py's pattern.

``override`` parameters come from the owner-configured
``AppProviderSettings`` row when present; when absent (None), every
function here falls back to exactly the env-var-driven behavior this
project has always had -- an env-only deployment that never touches the
Settings UI is unaffected.
"""

from sqlalchemy.orm import Session

from core.config import Settings
from providers.alpha_vantage import AlphaVantageMarketDataProvider
from providers.alpha_vantage_options import AlphaVantageOptionsProvider
from providers.base import EarningsCalendarProvider, MarketDataProvider, OptionsDataProvider
from providers.earningsapi import EarningsApiCalendarProvider
from providers.fallback import (
    EarningsCalendarProviderChain,
    MarketDataProviderChain,
    OptionsProviderChain,
)
from providers.finnhub import FinnhubEarningsCalendarProvider
from providers.fixture_options import FixtureOptionsProvider
from providers.ibkr_options import IBKROptionsProvider
from providers.ibkr_tws_options import IBKRTWSProvider
from providers.tiingo import TiingoMarketDataProvider
from services.secret_store import resolve_secret
from services.usage_instrumentation import instrument_data_provider

KNOWN_OPTIONS_PROVIDERS = ("alpha_vantage", "ibkr")
KNOWN_PRICE_HISTORY_PROVIDERS = ("tiingo", "alpha_vantage")
KNOWN_EARNINGS_CALENDAR_PROVIDERS = ("earningsapi", "finnhub")


class UnknownOptionsProviderError(Exception):
    pass


class MissingOptionsProviderConfigError(Exception):
    pass


def get_options_provider(
    settings: Settings, override: str | None = None, db: Session | None = None
) -> OptionsDataProvider:
    provider = (override or settings.options_provider).lower()

    if provider == "alpha_vantage":
        key = resolve_secret(settings, "alpha_vantage", db)
        if not key:
            raise MissingOptionsProviderConfigError(
                "options provider alpha_vantage requires ALPHA_VANTAGE_API_KEY"
            )
        return instrument_data_provider(
            AlphaVantageOptionsProvider(api_key=key), db, "alpha_vantage", "options"
        )

    if provider == "ibkr":
        return instrument_data_provider(_build_ibkr_transport(settings), db, "ibkr", "options")

    if provider == "fixture":
        # Deliberately excluded from KNOWN_OPTIONS_PROVIDERS and from
        # services/provider_settings.py's own validated list -- reachable
        # only via the raw OPTIONS_PROVIDER=fixture env var (see
        # providers/fixture_options.py), never selectable through the
        # Settings UI's owner-configured override.
        if db is None:
            raise MissingOptionsProviderConfigError(
                "options provider fixture requires a database session"
            )
        return FixtureOptionsProvider(db)

    raise UnknownOptionsProviderError(
        f"unknown options provider {provider!r} -- expected one of {KNOWN_OPTIONS_PROVIDERS}"
    )


def _build_options_provider(
    name: str, settings: Settings, db: Session | None = None
) -> OptionsDataProvider | None:
    """Returns None (never raises) when ``name`` isn't usable -- Alpha
    Vantage needs a key, IBKR needs nothing to construct (auth happens per
    call against the local Gateway, see providers/ibkr_client.py) so it's
    always considered "configured" here."""
    if name == "alpha_vantage":
        key = resolve_secret(settings, "alpha_vantage", db)
        if key:
            return instrument_data_provider(
                AlphaVantageOptionsProvider(api_key=key), db, "alpha_vantage", "options"
            )
        return None
    if name == "ibkr":
        return instrument_data_provider(_build_ibkr_transport(settings), db, "ibkr", "options")
    return None


# IBKR TWS Migration, Phase 3 readiness (Section 5) -- the backend
# process's ONE production TWS connection owner. Same real, non-
# picklable-object constraint and same module-level-global pattern as
# services/scheduler.py's own _shared_embedder/_shared_tws_health_probe:
# a TWSConnectionManager wraps a live socket, so it can only ever be
# constructed once and read fresh, never rebuilt per call. Set exactly
# once, at process startup, by api/main.py's lifespan; every real caller
# that already resolves "ibkr" through this factory -- the scheduler's
# decision/entry/exit-capture jobs, services/options_reconstruction.py,
# services/research_orchestration.py, and any future one -- transparently
# reuses the same real connection with zero call-site changes, instead of
# each independently opening (and never cleanly closing) its own socket
# under the identical client id, which is what this project's Phase 1/2
# code actually did: every _build_ibkr_transport(settings) call built a
# brand-new IBKRTWSProvider, relying on garbage collection alone to ever
# close the old socket -- harmless at the low frequency of the two daily
# capture jobs alone, but a real client-id-collision (IBKR error 326)
# risk the moment two in-process callers (e.g. a capture job and a
# concurrent reconstruction) raced to open a second connection under the
# same id before the first was ever explicitly closed.
_shared_tws_provider: IBKRTWSProvider | None = None


def set_shared_tws_provider(provider: IBKRTWSProvider | None) -> None:
    """Called once by api/main.py's lifespan at startup (with the one
    real, long-lived IBKRTWSProvider it constructs) and once at shutdown
    (with ``None``, after that provider's own ``shutdown()`` has already
    run) -- never per request, never per job. ``None`` is also this
    function's own implicit default (module import time) whenever TWS
    isn't the selected transport, or before the app has started at all
    (e.g. a standalone script or test importing this module directly) --
    _build_ibkr_transport below falls back to its pre-Phase-3 per-call
    construction in that case, so nothing here changes behavior for the
    Web transport (the real, current default) or for any caller outside
    the running backend process.
    """
    global _shared_tws_provider
    _shared_tws_provider = provider


def _build_ibkr_transport(settings: Settings) -> OptionsDataProvider:
    """IBKR TWS Migration Phase 1 -- the only place that knows a
    constructed "ibkr" options provider can speak either of IBKR's own two
    APIs. Defaults to "web" (the existing Client Portal Gateway adapter,
    UNCHANGED by this migration) unless ``settings.ibkr_provider`` is
    explicitly "tws" -- an unconfigured/unrecognized value also falls back
    to "web" rather than raising, so a typo in this new setting degrades
    to this project's existing, already-real behavior instead of breaking
    options collection outright. The official scheduler never sets this to
    "tws" during Phase 1 (see this migration's Phase 1 report, Section U) --
    reachable today only via an explicit env var or Settings-UI override,
    for experimentation.

    Phase 3 readiness (Section 5): the "tws" branch prefers the one
    shared, already-connected provider ``set_shared_tws_provider`` above
    stashed, when the calling process has one (the real running backend,
    once TWS is selected) -- only a caller with no shared instance
    available (a standalone script, a test, or a process that hasn't
    finished starting up yet) falls back to constructing its own,
    matching this function's exact pre-Section-5 behavior.
    """
    if settings.ibkr_provider.lower() == "tws":
        if _shared_tws_provider is not None:
            return _shared_tws_provider
        return IBKRTWSProvider(
            host=settings.ibkr_tws_host,
            port=settings.ibkr_tws_port,
            client_id=settings.ibkr_tws_client_id,
        )
    return IBKROptionsProvider(base_url=settings.ibkr_base_url)


def build_options_provider_chain(
    settings: Settings,
    primary_override: str | None = None,
    fallback_override: str | None = None,
    db: Session | None = None,
) -> OptionsDataProvider | None:
    """Real primary-then-fallback chain for options data. Returns None
    (never raises) when nothing is configured at all -- options collection
    is an optional preparation step (see
    services/research_orchestration.py::_prepare_options_chain), so an
    unconfigured deployment degrades that step to SKIPPED rather than
    failing the whole pipeline, exactly as before this function existed.
    With no override, matches the original single-provider behavior
    (``settings.options_provider``, no fallback at all).
    """
    order: tuple[str, ...]
    if primary_override is None and fallback_override is None:
        order = (settings.options_provider.lower(),)
    else:
        order = tuple(
            name
            for name in (primary_override, fallback_override)
            if name is not None and name in KNOWN_OPTIONS_PROVIDERS
        )

    providers: list[tuple[str, OptionsDataProvider]] = []
    for name in order:
        provider = _build_options_provider(name, settings, db)
        if provider is not None:
            providers.append((name, provider))

    if not providers:
        return None
    if len(providers) == 1:
        return providers[0][1]
    return OptionsProviderChain(providers)


def _build_market_data_provider(
    name: str, settings: Settings, db: Session | None = None
) -> MarketDataProvider | None:
    """Returns None (never raises) when ``name``'s credentials aren't
    configured -- the caller decides whether an unconfigured provider is
    silently skipped from a chain or reported as an error."""
    if name == "tiingo":
        key = resolve_secret(settings, "tiingo", db)
        if not key:
            return None
        return instrument_data_provider(
            TiingoMarketDataProvider(api_key=key), db, "tiingo", "price_history"
        )
    if name == "alpha_vantage":
        key = resolve_secret(settings, "alpha_vantage", db)
        if not key:
            return None
        return instrument_data_provider(
            AlphaVantageMarketDataProvider(api_key=key), db, "alpha_vantage", "price_history"
        )
    return None


def build_market_data_chain(
    settings: Settings,
    primary_override: str | None = None,
    fallback_override: str | None = None,
    db: Session | None = None,
) -> MarketDataProvider:
    """Real primary-then-fallback chain for daily price history. With no
    override, matches this project's original, unconfigurable default
    exactly: Tiingo first (if configured), Alpha Vantage after (if
    configured). An override reorders/restricts which real, configured
    providers participate -- an unconfigured provider named as primary or
    fallback is skipped, never fabricated as available; if that leaves
    nothing configured at all, raises the same real
    "no market data provider configured" error this project has always
    raised in that case.
    """
    order: tuple[str, ...]
    if primary_override is None and fallback_override is None:
        order = ("tiingo", "alpha_vantage")
    else:
        order = tuple(
            name
            for name in (primary_override, fallback_override)
            if name is not None and name in KNOWN_PRICE_HISTORY_PROVIDERS
        )

    providers: list[tuple[str, MarketDataProvider]] = []
    for name in order:
        provider = _build_market_data_provider(name, settings, db)
        if provider is not None:
            providers.append((name, provider))

    if not providers:
        raise RuntimeError(
            "no market data provider configured — set TIINGO_API_KEY or ALPHA_VANTAGE_API_KEY"
        )
    return MarketDataProviderChain(providers)


def _build_earnings_calendar_provider(
    name: str, settings: Settings, db: Session | None = None
) -> EarningsCalendarProvider | None:
    """Returns None (never raises) when ``name``'s credentials aren't
    configured -- same shape as _build_options_provider/
    _build_market_data_provider above."""
    if name == "earningsapi":
        key = resolve_secret(settings, "earningsapi", db)
        if not key:
            return None
        return instrument_data_provider(
            EarningsApiCalendarProvider(api_key=key), db, "earningsapi", "earnings_calendar"
        )
    if name == "finnhub":
        key = resolve_secret(settings, "finnhub", db)
        if not key:
            return None
        return instrument_data_provider(
            FinnhubEarningsCalendarProvider(api_key=key), db, "finnhub", "earnings_calendar"
        )
    return None


def build_earnings_calendar_provider(
    settings: Settings, db: Session | None = None
) -> EarningsCalendarProvider | None:
    """EarningsAPI.com primary, Finnhub fallback (see
    EARNINGS_CALENDAR_PROVIDER_ARCHITECTURE_REVIEW.md) -- Finnhub's own
    free tier was confirmed live, against this project's own real data,
    to return far-future placeholder dates once its near-term coverage
    ran out, so it was demoted rather than dropped: it's still a real,
    working fallback if EarningsAPI.com itself is unreachable or
    unconfigured. Returns None (never raises) when neither is configured,
    matching build_options_provider_chain's own precedent -- the calendar
    sync job (services/earnings_calendar_sync.py) decides whether that's
    a hard failure or a skip.

    Unlike build_options_provider_chain/build_market_data_chain, a
    single configured provider is still wrapped in
    EarningsCalendarProviderChain rather than returned unwrapped -- a
    deliberate, small deviation from those two functions' own precedent,
    made so ``last_actual_provider``/``last_requested_provider`` are
    always real, populated attributes the scheduler job can log from
    (see services/scheduler.py::run_earnings_calendar_sync_job), instead
    of only when a fallback happens to exist. A single-provider chain
    behaves identically to that provider called directly -- it never
    changes what real data is returned, only what's observable about
    which provider returned it."""
    providers: list[tuple[str, EarningsCalendarProvider]] = []
    for name in KNOWN_EARNINGS_CALENDAR_PROVIDERS:
        provider = _build_earnings_calendar_provider(name, settings, db)
        if provider is not None:
            providers.append((name, provider))

    if not providers:
        return None
    return EarningsCalendarProviderChain(providers)
