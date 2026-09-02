"""Generic fallback chain for any *DataProvider ABC: try providers in order,
fall through to the next on failure, raise only if all fail. This is the
"provider error mapping" / graceful-degradation pattern applied to
MarketDataProvider and OptionsDataProvider -- the same shape works for any
other provider interface without duplicating logic per provider type.

Both chains record real fallback provenance after every call
(``last_requested_provider`` / ``last_actual_provider`` /
``last_fallback_reason``) -- never silently switch providers without this
being observable by the caller (see services/provider_status.py, which
turns this into a persisted ProviderHealthEvent when a fallback occurs).
"""

import logging
from collections.abc import Callable
from datetime import date, datetime

from observability.redact import redact
from providers.base import EarningsCalendarProvider, MarketDataProvider, OptionsDataProvider
from providers.types import (
    FinnhubCalendarEntry,
    FinnhubCompanyProfile,
    KnownContract,
    OHLCBar,
    OptionQuote,
    SelectedLeg,
    SnapshotAttempt,
    UnderlyingQuote,
)

log = logging.getLogger(__name__)


class AllProvidersFailedError(Exception):
    def __init__(self, errors: list[tuple[str, Exception]]) -> None:
        self.errors = errors
        detail = "; ".join(f"{name}: {redact(str(exc))}" for name, exc in errors)
        super().__init__(f"all providers failed: {detail}")


class MarketDataProviderChain(MarketDataProvider):
    def __init__(self, providers: list[tuple[str, MarketDataProvider]]) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = providers
        self.last_requested_provider: str | None = None
        self.last_actual_provider: str | None = None
        self.last_fallback_reason: str | None = None

    def get_daily_bars(self, ticker: str, start: date, end: date) -> list[OHLCBar]:
        self.last_requested_provider = self._providers[0][0]
        errors: list[tuple[str, Exception]] = []
        for name, provider in self._providers:
            try:
                bars = provider.get_daily_bars(ticker, start, end)
                self._record_success(name, errors)
                return bars
            except Exception as exc:  # noqa: BLE001 — deliberately broad: any
                # provider failure should fall through to the next provider,
                # not just the exception types we happened to anticipate.
                log.warning("provider %s failed for %s: %s", name, ticker, redact(str(exc)))
                errors.append((name, exc))
        raise AllProvidersFailedError(errors)

    def _record_success(self, name: str, prior_errors: list[tuple[str, Exception]]) -> None:
        self.last_actual_provider = name
        if prior_errors:
            failed_name, failed_exc = prior_errors[-1]
            self.last_fallback_reason = f"{failed_name} failed: {redact(str(failed_exc))}"
        else:
            self.last_fallback_reason = None


class OptionsProviderChain(OptionsDataProvider):
    def __init__(self, providers: list[tuple[str, OptionsDataProvider]]) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = providers
        self.last_requested_provider: str | None = None
        self.last_actual_provider: str | None = None
        self.last_fallback_reason: str | None = None

    def get_option_chain(
        self,
        ticker: str,
        as_of: datetime,
        expiration: date | None = None,
        reference_date: date | None = None,
        earnings_anchored: bool = True,
    ) -> list[OptionQuote]:
        self.last_requested_provider = self._providers[0][0]
        errors: list[tuple[str, Exception]] = []
        for name, provider in self._providers:
            try:
                quotes = provider.get_option_chain(
                    ticker,
                    as_of,
                    expiration=expiration,
                    reference_date=reference_date,
                    earnings_anchored=earnings_anchored,
                )
                self.last_actual_provider = name
                if errors:
                    failed_name, failed_exc = errors[-1]
                    self.last_fallback_reason = f"{failed_name} failed: {redact(str(failed_exc))}"
                else:
                    self.last_fallback_reason = None
                return quotes
            except Exception as exc:  # noqa: BLE001 — same rationale as MarketDataProviderChain
                log.warning("provider %s failed for %s: %s", name, ticker, redact(str(exc)))
                errors.append((name, exc))
        raise AllProvidersFailedError(errors)

    def get_underlying_quote(self, ticker: str) -> UnderlyingQuote | None:
        """Same primary-then-fallback shape as get_option_chain above, with
        one difference: a provider reporting "no live underlying data" is
        not an exception here, it's an honest ``None`` return (see
        OptionsDataProvider.get_underlying_quote's own docstring) -- so
        both an exception and a ``None`` result fall through to the next
        provider. If every provider in the chain comes back empty, this
        returns ``None`` too rather than raising, matching the same
        "no live underlying quote" contract any single provider has."""
        self.last_requested_provider = self._providers[0][0]
        errors: list[tuple[str, Exception]] = []
        for name, provider in self._providers:
            try:
                quote = provider.get_underlying_quote(ticker)
            except Exception as exc:  # noqa: BLE001 — same rationale as get_option_chain
                log.warning("provider %s failed for %s: %s", name, ticker, redact(str(exc)))
                errors.append((name, exc))
                continue
            if quote is None:
                errors.append((name, RuntimeError("no live underlying quote available")))
                continue
            self.last_actual_provider = name
            if errors:
                failed_name, failed_exc = errors[-1]
                self.last_fallback_reason = f"{failed_name} failed: {redact(str(failed_exc))}"
            else:
                self.last_fallback_reason = None
            return quote
        return None

    def get_quotes_for_known_contracts(
        self,
        ticker: str,
        contracts: list[KnownContract],
        expiration: date,
        as_of: datetime,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[OptionQuote]:
        """Same primary-then-fallback shape as get_option_chain above --
        an empty list is a legitimate, honestly reported result (matching
        get_option_chain's own precedent), so only a real exception falls
        through to the next provider, never an empty-but-successful
        response."""
        self.last_requested_provider = self._providers[0][0]
        errors: list[tuple[str, Exception]] = []
        for name, provider in self._providers:
            try:
                quotes = provider.get_quotes_for_known_contracts(
                    ticker, contracts, expiration, as_of, on_attempt=on_attempt
                )
                self.last_actual_provider = name
                if errors:
                    failed_name, failed_exc = errors[-1]
                    self.last_fallback_reason = f"{failed_name} failed: {redact(str(failed_exc))}"
                else:
                    self.last_fallback_reason = None
                return quotes
            except Exception as exc:  # noqa: BLE001 — same rationale as get_option_chain
                log.warning("provider %s failed for %s: %s", name, ticker, redact(str(exc)))
                errors.append((name, exc))
        raise AllProvidersFailedError(errors)

    def get_quotes_for_selected_legs(
        self,
        ticker: str,
        legs: list[SelectedLeg],
        expiration: date,
        as_of: datetime,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[OptionQuote]:
        """IBKR execution-observability hardening (2026-08-26) -- without
        this override, OptionsDataProvider's own default (delegates to
        self.get_option_chain, i.e. THIS chain's own full-discovery
        method) would silently undo Section 7's entry-capture efficiency
        fix the moment two options providers are ever configured: every
        real request would go through full ATM-window rediscovery again,
        on whichever provider is primary, instead of each underlying
        provider's own efficient exact-leg resolution. Same primary-
        then-fallback shape as get_quotes_for_known_contracts above."""
        self.last_requested_provider = self._providers[0][0]
        errors: list[tuple[str, Exception]] = []
        for name, provider in self._providers:
            try:
                quotes = provider.get_quotes_for_selected_legs(
                    ticker, legs, expiration, as_of, on_attempt=on_attempt
                )
                self.last_actual_provider = name
                if errors:
                    failed_name, failed_exc = errors[-1]
                    self.last_fallback_reason = f"{failed_name} failed: {redact(str(failed_exc))}"
                else:
                    self.last_fallback_reason = None
                return quotes
            except Exception as exc:  # noqa: BLE001 — same rationale as get_option_chain
                log.warning("provider %s failed for %s: %s", name, ticker, redact(str(exc)))
                errors.append((name, exc))
        raise AllProvidersFailedError(errors)


class EarningsCalendarProviderChain(EarningsCalendarProvider):
    """Same primary-then-fallback shape as MarketDataProviderChain/
    OptionsProviderChain above -- EarningsAPI.com primary, Finnhub
    fallback (see EARNINGS_CALENDAR_PROVIDER_ARCHITECTURE_REVIEW.md).
    Fallback triggers on any real exception from the primary: timeout,
    HTTP/auth error, rate limit, or a malformed response the provider
    itself couldn't parse -- providers/earningsapi.py and
    providers/finnhub.py both already turn every one of those into a
    real exception rather than a silent empty result, so "any Exception"
    here is the correct, complete trigger set, not an approximation."""

    def __init__(self, providers: list[tuple[str, EarningsCalendarProvider]]) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = providers
        self.last_requested_provider: str | None = None
        self.last_actual_provider: str | None = None
        self.last_fallback_reason: str | None = None

    def get_earnings_calendar(self, from_date: date, to_date: date) -> list[FinnhubCalendarEntry]:
        self.last_requested_provider = self._providers[0][0]
        errors: list[tuple[str, Exception]] = []
        for name, provider in self._providers:
            try:
                entries = provider.get_earnings_calendar(from_date, to_date)
                self._record_success(name, errors)
                return entries
            except Exception as exc:  # noqa: BLE001 — same rationale as get_option_chain
                log.warning(
                    "earnings calendar provider %s failed for [%s, %s]: %s",
                    name,
                    from_date,
                    to_date,
                    redact(str(exc)),
                )
                errors.append((name, exc))
        raise AllProvidersFailedError(errors)

    def get_company_profile(self, symbol: str) -> FinnhubCompanyProfile | None:
        """Same "exception AND None both fall through" contract as
        OptionsProviderChain.get_underlying_quote above: a provider
        reporting "unknown symbol" honestly (None) doesn't mean no
        provider in the chain knows this symbol -- only that this one
        doesn't. Returns None only once every provider has said so."""
        self.last_requested_provider = self._providers[0][0]
        errors: list[tuple[str, Exception]] = []
        for name, provider in self._providers:
            try:
                profile = provider.get_company_profile(symbol)
            except Exception as exc:  # noqa: BLE001 — same rationale as get_option_chain
                log.warning(
                    "earnings calendar provider %s failed for %s: %s",
                    name,
                    symbol,
                    redact(str(exc)),
                )
                errors.append((name, exc))
                continue
            if profile is None:
                errors.append((name, RuntimeError("no company profile available")))
                continue
            self._record_success(name, errors)
            return profile
        return None

    def _record_success(self, name: str, prior_errors: list[tuple[str, Exception]]) -> None:
        self.last_actual_provider = name
        if prior_errors:
            failed_name, failed_exc = prior_errors[-1]
            self.last_fallback_reason = f"{failed_name} failed: {redact(str(failed_exc))}"
        else:
            self.last_fallback_reason = None
