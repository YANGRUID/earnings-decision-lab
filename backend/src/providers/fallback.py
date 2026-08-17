"""Generic fallback chain for any *DataProvider ABC: try providers in order,
fall through to the next on failure, raise only if all fail. This is the
"provider error mapping" / graceful-degradation pattern applied to
MarketDataProvider today; the same shape works for any other provider
interface without duplicating logic per provider type.
"""

import logging
from datetime import date

from observability.redact import redact
from providers.base import MarketDataProvider
from providers.types import OHLCBar

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

    def get_daily_bars(self, ticker: str, start: date, end: date) -> list[OHLCBar]:
        errors: list[tuple[str, Exception]] = []
        for name, provider in self._providers:
            try:
                return provider.get_daily_bars(ticker, start, end)
            except Exception as exc:  # noqa: BLE001 — deliberately broad: any
                # provider failure should fall through to the next provider,
                # not just the exception types we happened to anticipate.
                log.warning("provider %s failed for %s: %s", name, ticker, redact(str(exc)))
                errors.append((name, exc))
        raise AllProvidersFailedError(errors)
