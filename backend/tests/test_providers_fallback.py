from datetime import date

import pytest

from providers.base import MarketDataProvider
from providers.fallback import AllProvidersFailedError, MarketDataProviderChain
from providers.types import OHLCBar


class _FailingProvider(MarketDataProvider):
    def get_daily_bars(self, ticker, start, end):
        raise RuntimeError("simulated provider failure")


class _WorkingProvider(MarketDataProvider):
    def get_daily_bars(self, ticker, start, end):
        return [
            OHLCBar(
                ticker=ticker,
                trade_date=start,
                open=1,
                high=1,
                low=1,
                close=1,
                volume=1,
                source_provider="working",
                retrieved_at="2025-01-01T00:00:00Z",
            )
        ]


def test_falls_through_to_next_provider_on_failure():
    chain = MarketDataProviderChain(
        [("primary", _FailingProvider()), ("fallback", _WorkingProvider())]
    )

    bars = chain.get_daily_bars("MU", date(2025, 1, 1), date(2025, 1, 2))

    assert bars[0].source_provider == "working"


def test_uses_primary_when_it_works():
    chain = MarketDataProviderChain(
        [("primary", _WorkingProvider()), ("fallback", _FailingProvider())]
    )

    bars = chain.get_daily_bars("MU", date(2025, 1, 1), date(2025, 1, 2))

    assert bars[0].source_provider == "working"


def test_raises_when_all_providers_fail():
    chain = MarketDataProviderChain(
        [("primary", _FailingProvider()), ("fallback", _FailingProvider())]
    )

    with pytest.raises(AllProvidersFailedError):
        chain.get_daily_bars("MU", date(2025, 1, 1), date(2025, 1, 2))


def test_requires_at_least_one_provider():
    with pytest.raises(ValueError):
        MarketDataProviderChain([])
