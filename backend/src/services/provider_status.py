"""Real, backend-owned provider status: what's configured, what's actually
been used successfully (derived from real ingested rows, never a log
claiming success), what's recently failed (from real ProviderHealthEvent
rows -- see models/provider_health_event.py), and a hand-reviewed
capability matrix -- the only place this project claims "provider X can do
Y", so it never drifts from what the real adapters actually implement.

Domains: price_history, earnings_estimates, filings, options, llm. Each
lists only the providers that are REAL adapters in this codebase (see
providers/ directory) -- never a fabricated "planned" integration
presented as available.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.config import Settings
from models.app_provider_settings import AppProviderSettings
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.enums import ProviderHealthStatus
from models.filing import Filing
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.provider_health_event import ProviderHealthEvent
from services.provider_settings import get_app_provider_settings

PRICE_HISTORY_PROVIDERS = ("tiingo", "alpha_vantage")
EARNINGS_ESTIMATES_PROVIDERS = ("alpha_vantage",)
FILINGS_PROVIDERS = ("sec_edgar",)
OPTIONS_PROVIDERS = ("ibkr", "alpha_vantage")
LLM_PROVIDERS = ("deepseek", "openai", "anthropic", "openai_compatible")


@dataclass(frozen=True)
class ProviderCapabilities:
    prices: bool = False
    earnings_estimates: bool = False
    filings: bool = False
    options: bool = False
    greeks: bool = False
    ai: bool = False


# The single, hand-reviewed source of truth for "what can provider X
# actually do" -- matches the real adapters in providers/ and
# services/llm/ as of this writing, not aspirational. Update this alongside
# any real adapter change, never let it silently drift.
PROVIDER_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "tiingo": ProviderCapabilities(prices=True),
    "alpha_vantage": ProviderCapabilities(
        prices=True, earnings_estimates=True, options=True, greeks=True
    ),
    "sec_edgar": ProviderCapabilities(filings=True),
    "ibkr": ProviderCapabilities(options=True, greeks=True),
    "deepseek": ProviderCapabilities(ai=True),
    "openai": ProviderCapabilities(ai=True),
    "anthropic": ProviderCapabilities(ai=True),
    "openai_compatible": ProviderCapabilities(ai=True),
}

# Known, documented entitlement caveats -- real facts confirmed live during
# earlier phases (see docs/data_sources.md), not guessed.
PROVIDER_ENTITLEMENT_NOTES: dict[str, str] = {
    "alpha_vantage": (
        "REALTIME_OPTIONS/HISTORICAL_OPTIONS are premium-gated on this project's plan "
        "(confirmed live) -- price and earnings-estimate endpoints are not."
    ),
}

DOMAIN_PROVIDERS: dict[str, tuple[str, ...]] = {
    "price_history": PRICE_HISTORY_PROVIDERS,
    "earnings_estimates": EARNINGS_ESTIMATES_PROVIDERS,
    "filings": FILINGS_PROVIDERS,
    "options": OPTIONS_PROVIDERS,
    "llm": LLM_PROVIDERS,
}


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    domain: str
    configured: bool
    masked_key: str | None
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_status: str | None
    last_error_detail: str | None
    entitlement_note: str | None
    capabilities: ProviderCapabilities


@dataclass(frozen=True)
class DomainStatus:
    domain: str
    primary: str | None
    fallback: str | None
    primary_is_override: bool
    fallback_is_override: bool
    providers: list[ProviderStatus] = field(default_factory=list)


def _mask(key: str | None) -> str | None:
    if not key:
        return None
    if len(key) <= 4:
        return "•" * len(key)
    return "••••••••" + key[-4:]


def _raw_key(provider: str, settings: Settings) -> str | None:
    return {
        "tiingo": settings.tiingo_api_key,
        "alpha_vantage": settings.alpha_vantage_api_key,
        "deepseek": settings.deepseek_api_key,
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
        "openai_compatible": settings.openai_compatible_api_key,
    }.get(provider)


def _is_configured(provider: str, settings: Settings) -> bool:
    if provider in ("sec_edgar", "ibkr"):
        # No API key concept: SEC EDGAR requires only a contact User-Agent
        # (see core.config.Settings.sec_edgar_user_agent, always has a
        # default); IBKR auth happens per-call against the user's own local
        # Gateway, never a key this project holds.
        return True
    return bool(_raw_key(provider, settings))


def _last_health_event(
    db: Session, provider: str, domain: str, exclude_connected: bool = False
) -> ProviderHealthEvent | None:
    query = db.query(ProviderHealthEvent).filter(
        ProviderHealthEvent.provider == provider, ProviderHealthEvent.domain == domain
    )
    if exclude_connected:
        query = query.filter(ProviderHealthEvent.status != ProviderHealthStatus.CONNECTED)
    return query.order_by(ProviderHealthEvent.occurred_at.desc()).first()


def _last_success_at(db: Session, provider: str, domain: str) -> datetime | None:
    """Derived from real ingested rows, not a log -- a persisted PriceBar/
    OptionsSnapshot/etc. *is* proof of a real successful call; a log row
    claiming success would be a weaker, duplicated signal."""
    if domain == "price_history":
        return db.query(func.max(PriceBar.retrieved_at)).filter(
            PriceBar.source_provider == provider
        ).scalar()
    if domain == "earnings_estimates":
        return db.query(func.max(EarningsEstimateSnapshot.retrieved_at)).filter(
            EarningsEstimateSnapshot.source_provider == provider
        ).scalar()
    if domain == "filings":
        return db.query(func.max(Filing.retrieved_at)).scalar()
    if domain == "options":
        return db.query(func.max(OptionsSnapshot.retrieved_at)).filter(
            OptionsSnapshot.source_provider == provider
        ).scalar()
    if domain == "llm":
        # No persisted artifact of a successful generation exists today
        # (a thesis/answer isn't tagged with which provider produced it at
        # the row level) -- last real signal available is an explicit
        # "Test Connection" success, from provider_health_event.
        event = _last_health_event(db, provider, domain)
        if event is not None and event.status == ProviderHealthStatus.CONNECTED:
            return event.occurred_at
        return None
    return None


def _provider_status(
    db: Session, settings: Settings, provider: str, domain: str
) -> ProviderStatus:
    error_event = _last_health_event(db, provider, domain, exclude_connected=True)
    return ProviderStatus(
        provider=provider,
        domain=domain,
        configured=_is_configured(provider, settings),
        masked_key=_mask(_raw_key(provider, settings)),
        last_success_at=_last_success_at(db, provider, domain),
        last_error_at=error_event.occurred_at if error_event else None,
        last_error_status=error_event.status.value if error_event else None,
        last_error_detail=error_event.detail if error_event else None,
        entitlement_note=PROVIDER_ENTITLEMENT_NOTES.get(provider),
        capabilities=PROVIDER_CAPABILITIES[provider],
    )


def _resolve_price_history(overrides: AppProviderSettings) -> tuple[str, str | None, bool, bool]:
    primary = overrides.price_history_primary or "tiingo"
    fallback = overrides.price_history_fallback or (
        "alpha_vantage" if overrides.price_history_primary is None else None
    )
    return primary, fallback, overrides.price_history_primary is not None, (
        overrides.price_history_fallback is not None
    )


def _resolve_options(
    overrides: AppProviderSettings, settings: Settings
) -> tuple[str, str | None, bool, bool]:
    primary = overrides.options_primary or settings.options_provider
    fallback = overrides.options_fallback
    return primary, fallback, overrides.options_primary is not None, (
        overrides.options_fallback is not None
    )


def get_provider_dashboard(db: Session, settings: Settings) -> list[DomainStatus]:
    """The full, real Data Provider Control Center payload: one
    DomainStatus per real domain, each carrying every real provider that
    exists for it (see DOMAIN_PROVIDERS) with real, current status."""
    overrides = get_app_provider_settings(db)

    price_primary, price_fallback, price_primary_override, price_fallback_override = (
        _resolve_price_history(overrides)
    )
    options_primary, options_fallback, options_primary_override, options_fallback_override = (
        _resolve_options(overrides, settings)
    )
    llm_primary = overrides.llm_provider or settings.llm_provider

    domains = [
        DomainStatus(
            domain="price_history",
            primary=price_primary,
            fallback=price_fallback,
            primary_is_override=price_primary_override,
            fallback_is_override=price_fallback_override,
            providers=[
                _provider_status(db, settings, p, "price_history")
                for p in PRICE_HISTORY_PROVIDERS
            ],
        ),
        DomainStatus(
            domain="earnings_estimates",
            primary=EARNINGS_ESTIMATES_PROVIDERS[0],
            fallback=None,
            primary_is_override=False,
            fallback_is_override=False,
            providers=[
                _provider_status(db, settings, p, "earnings_estimates")
                for p in EARNINGS_ESTIMATES_PROVIDERS
            ],
        ),
        DomainStatus(
            domain="filings",
            primary=FILINGS_PROVIDERS[0],
            fallback=None,
            primary_is_override=False,
            fallback_is_override=False,
            providers=[_provider_status(db, settings, p, "filings") for p in FILINGS_PROVIDERS],
        ),
        DomainStatus(
            domain="options",
            primary=options_primary,
            fallback=options_fallback,
            primary_is_override=options_primary_override,
            fallback_is_override=options_fallback_override,
            providers=[_provider_status(db, settings, p, "options") for p in OPTIONS_PROVIDERS],
        ),
        DomainStatus(
            domain="llm",
            primary=llm_primary,
            fallback=None,
            primary_is_override=overrides.llm_provider is not None,
            fallback_is_override=False,
            providers=[_provider_status(db, settings, p, "llm") for p in LLM_PROVIDERS],
        ),
    ]
    return domains


def record_health_event(
    db: Session,
    provider: str,
    domain: str,
    status: ProviderHealthStatus,
    detail: str | None,
    occurred_at: datetime,
) -> ProviderHealthEvent:
    event = ProviderHealthEvent(
        provider=provider, domain=domain, status=status, detail=detail, occurred_at=occurred_at
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event
