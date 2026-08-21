from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from providers.base import MarketDataProvider, OptionsDataProvider
from providers.fallback import (
    AllProvidersFailedError,
    MarketDataProviderChain,
    OptionsProviderChain,
)
from providers.types import OHLCBar, OptionQuote, UnderlyingQuote


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


class TestMarketDataProviderChainProvenance:
    def test_records_actual_provider_and_no_fallback_reason_when_primary_works(self):
        chain = MarketDataProviderChain(
            [("primary", _WorkingProvider()), ("fallback", _FailingProvider())]
        )
        chain.get_daily_bars("MU", date(2025, 1, 1), date(2025, 1, 2))
        assert chain.last_requested_provider == "primary"
        assert chain.last_actual_provider == "primary"
        assert chain.last_fallback_reason is None

    def test_records_a_real_fallback_reason_when_primary_fails(self):
        chain = MarketDataProviderChain(
            [("primary", _FailingProvider()), ("fallback", _WorkingProvider())]
        )
        chain.get_daily_bars("MU", date(2025, 1, 1), date(2025, 1, 2))
        assert chain.last_requested_provider == "primary"
        assert chain.last_actual_provider == "fallback"
        assert "primary" in chain.last_fallback_reason
        assert "simulated provider failure" in chain.last_fallback_reason

    def test_provenance_is_none_before_any_call_is_made(self):
        chain = MarketDataProviderChain([("primary", _WorkingProvider())])
        assert chain.last_requested_provider is None
        assert chain.last_actual_provider is None
        assert chain.last_fallback_reason is None


class _FailingOptionsProvider(OptionsDataProvider):
    def get_option_chain(
        self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
    ):
        raise RuntimeError("simulated options provider failure")


class _WorkingOptionsProvider(OptionsDataProvider):
    def get_option_chain(
        self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
    ):
        return [
            OptionQuote(
                ticker=ticker,
                snapshot_timestamp=as_of,
                expiration_date=date(2026, 1, 16),
                strike=Decimal("100"),
                option_type="call",
                source_provider="working",
                retrieved_at=as_of,
            )
        ]

    def get_underlying_quote(self, ticker):
        return UnderlyingQuote(
            ticker=ticker,
            price=Decimal("100"),
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            source_provider="working",
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


class _RaisingUnderlyingOptionsProvider(OptionsDataProvider):
    """Has a working option chain but a live underlying fetch that raises
    -- distinct from a provider that simply doesn't override
    get_underlying_quote at all (which honestly returns None, the
    OptionsDataProvider default, not an exception)."""

    def get_option_chain(
        self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
    ):
        return []

    def get_underlying_quote(self, ticker):
        raise RuntimeError("simulated underlying quote failure")


class TestOptionsProviderChain:
    def test_requires_at_least_one_provider(self):
        with pytest.raises(ValueError):
            OptionsProviderChain([])

    def test_uses_primary_when_it_works(self):
        chain = OptionsProviderChain(
            [("primary", _WorkingOptionsProvider()), ("fallback", _FailingOptionsProvider())]
        )
        quotes = chain.get_option_chain("NVDA", datetime.now(UTC))
        assert quotes[0].source_provider == "working"
        assert chain.last_requested_provider == "primary"
        assert chain.last_actual_provider == "primary"
        assert chain.last_fallback_reason is None

    def test_falls_through_to_next_provider_on_failure(self):
        chain = OptionsProviderChain(
            [("primary", _FailingOptionsProvider()), ("fallback", _WorkingOptionsProvider())]
        )
        quotes = chain.get_option_chain("NVDA", datetime.now(UTC))
        assert quotes[0].source_provider == "working"
        assert chain.last_requested_provider == "primary"
        assert chain.last_actual_provider == "fallback"
        assert "simulated options provider failure" in chain.last_fallback_reason

    def test_raises_when_all_providers_fail(self):
        chain = OptionsProviderChain(
            [("primary", _FailingOptionsProvider()), ("fallback", _FailingOptionsProvider())]
        )
        with pytest.raises(AllProvidersFailedError):
            chain.get_option_chain("NVDA", datetime.now(UTC))


class TestOptionsProviderChainUnderlyingQuote:
    """Phase 4.4 hardening -- get_underlying_quote must fall through the
    chain the same way get_option_chain does, so the official benchmark
    entry capture (services/benchmark_entry_capture.py) gets a real live
    provider's underlying data through the same primary-then-fallback
    configuration used everywhere else, not silently the ABC default
    (None) regardless of which provider is actually behind the chain."""

    def test_uses_primary_when_it_works(self):
        chain = OptionsProviderChain(
            [("primary", _WorkingOptionsProvider()), ("fallback", _FailingOptionsProvider())]
        )
        quote = chain.get_underlying_quote("NVDA")
        assert quote is not None
        assert quote.source_provider == "working"
        assert chain.last_actual_provider == "primary"
        assert chain.last_fallback_reason is None

    def test_falls_through_on_exception(self):
        chain = OptionsProviderChain(
            [
                ("primary", _RaisingUnderlyingOptionsProvider()),
                ("fallback", _WorkingOptionsProvider()),
            ]
        )
        quote = chain.get_underlying_quote("NVDA")
        assert quote is not None
        assert quote.source_provider == "working"
        assert chain.last_actual_provider == "fallback"
        assert "simulated underlying quote failure" in chain.last_fallback_reason

    def test_falls_through_on_none(self):
        """A provider with no live underlying capability at all (the
        OptionsDataProvider default) is not an exception -- it's an
        honest None -- but must still fall through to the next provider
        exactly like a raised exception would."""
        chain = OptionsProviderChain(
            [("primary", _FailingOptionsProvider()), ("fallback", _WorkingOptionsProvider())]
        )
        quote = chain.get_underlying_quote("NVDA")
        assert quote is not None
        assert quote.source_provider == "working"
        assert chain.last_actual_provider == "fallback"

    def test_returns_none_when_every_provider_lacks_underlying_data(self):
        """Never raises for "unsupported everywhere" -- matches the same
        Optional[UnderlyingQuote] contract a single provider has."""
        chain = OptionsProviderChain(
            [("primary", _FailingOptionsProvider()), ("fallback", _FailingOptionsProvider())]
        )
        quote = chain.get_underlying_quote("NVDA")
        assert quote is None
