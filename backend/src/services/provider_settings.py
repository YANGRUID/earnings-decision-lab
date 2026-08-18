"""Owner-configured provider selection overrides -- the single place that
reads/writes ``AppProviderSettings`` (a one-row table; see
models/app_provider_settings.py). Read fresh on every request that
constructs a provider, never cached like ``core.config.Settings`` --
switching a provider in the UI must take effect on the very next request,
not after a restart.

The known-real-provider lists below are the only place this project
enumerates "which providers actually exist for domain X" for validation
purposes -- deliberately small and hand-maintained, not derived from
anything dynamic, so a made-up provider name is rejected rather than
silently accepted.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from models.app_provider_settings import AppProviderSettings

KNOWN_PRICE_HISTORY_PROVIDERS = ("tiingo", "alpha_vantage")
KNOWN_OPTIONS_PROVIDERS = ("ibkr", "alpha_vantage")
KNOWN_LLM_PROVIDERS = ("deepseek", "openai", "anthropic", "openai_compatible")


class UnknownProviderSelectionError(Exception):
    def __init__(self, domain: str, value: str, known: tuple[str, ...]) -> None:
        self.domain = domain
        self.value = value
        self.known = known
        super().__init__(f"{value!r} is not a real {domain} provider -- expected one of {known}")


def get_app_provider_settings(db: Session) -> AppProviderSettings:
    """Get-or-create the singleton settings row. Never raises on a fresh
    database -- an app that's never had its providers configured through
    the UI gets an all-null row, meaning every domain falls back to its
    real env-var default (see services/provider_settings.py's
    resolve_* helpers below)."""
    row = db.get(AppProviderSettings, 1)
    if row is None:
        row = AppProviderSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@dataclass(frozen=True)
class ProviderSettingsUpdate:
    price_history_primary: str | None = None
    price_history_fallback: str | None = None
    options_primary: str | None = None
    options_fallback: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    # Distinguishes "leave this field alone" from "explicitly clear it back
    # to the env-var default (None)" -- a plain dataclass with all-Optional
    # fields can't tell those apart, since None is a valid target value.
    clear_price_history_fallback: bool = False
    clear_options_fallback: bool = False


def update_app_provider_settings(
    db: Session, update: ProviderSettingsUpdate
) -> AppProviderSettings:
    """Validates every non-null field against the real known-provider
    lists (never persists a made-up provider name), then applies it to the
    singleton row. Only touches fields the caller actually set."""
    if update.price_history_primary is not None:
        _validate("price_history", update.price_history_primary, KNOWN_PRICE_HISTORY_PROVIDERS)
    if update.price_history_fallback is not None:
        _validate("price_history", update.price_history_fallback, KNOWN_PRICE_HISTORY_PROVIDERS)
    if update.options_primary is not None:
        _validate("options", update.options_primary, KNOWN_OPTIONS_PROVIDERS)
    if update.options_fallback is not None:
        _validate("options", update.options_fallback, KNOWN_OPTIONS_PROVIDERS)
    if update.llm_provider is not None:
        _validate("llm", update.llm_provider, KNOWN_LLM_PROVIDERS)

    row = get_app_provider_settings(db)
    if update.price_history_primary is not None:
        row.price_history_primary = update.price_history_primary
    if update.price_history_fallback is not None:
        row.price_history_fallback = update.price_history_fallback
    if update.clear_price_history_fallback:
        row.price_history_fallback = None
    if update.options_primary is not None:
        row.options_primary = update.options_primary
    if update.options_fallback is not None:
        row.options_fallback = update.options_fallback
    if update.clear_options_fallback:
        row.options_fallback = None
    if update.llm_provider is not None:
        row.llm_provider = update.llm_provider
    if update.llm_model is not None:
        row.llm_model = update.llm_model
    row.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return row


def _validate(domain: str, value: str, known: tuple[str, ...]) -> None:
    if value not in known:
        raise UnknownProviderSelectionError(domain, value, known)
