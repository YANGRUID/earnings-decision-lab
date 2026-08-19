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
from providers.base import MarketDataProvider, OptionsDataProvider
from providers.fallback import MarketDataProviderChain, OptionsProviderChain
from providers.ibkr_options import IBKROptionsProvider
from providers.tiingo import TiingoMarketDataProvider
from services.secret_store import resolve_secret
from services.usage_instrumentation import instrument_data_provider

KNOWN_OPTIONS_PROVIDERS = ("alpha_vantage", "ibkr")
KNOWN_PRICE_HISTORY_PROVIDERS = ("tiingo", "alpha_vantage")


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
        return instrument_data_provider(
            IBKROptionsProvider(base_url=settings.ibkr_base_url), db, "ibkr", "options"
        )

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
        return instrument_data_provider(
            IBKROptionsProvider(base_url=settings.ibkr_base_url), db, "ibkr", "options"
        )
    return None


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
