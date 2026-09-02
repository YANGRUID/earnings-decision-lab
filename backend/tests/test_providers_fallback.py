from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from providers.base import EarningsCalendarProvider, MarketDataProvider, OptionsDataProvider
from providers.fallback import (
    AllProvidersFailedError,
    EarningsCalendarProviderChain,
    MarketDataProviderChain,
    OptionsProviderChain,
)
from providers.types import (
    FinnhubCalendarEntry,
    FinnhubCompanyProfile,
    KnownContract,
    OHLCBar,
    OptionQuote,
    SelectedLeg,
    UnderlyingQuote,
)


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

    def get_quotes_for_known_contracts(self, ticker, contracts, expiration, as_of, on_attempt=None):
        return [
            OptionQuote(
                ticker=ticker,
                snapshot_timestamp=as_of,
                expiration_date=expiration,
                strike=Decimal("100"),
                option_type="call",
                bid=Decimal("1.90"),
                ask=Decimal("2.10"),
                external_contract_id="12345",
                source_provider="working",
                retrieved_at=as_of,
            )
        ]


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


class _RaisingKnownContractsOptionsProvider(OptionsDataProvider):
    """Has a working option chain but a get_quotes_for_known_contracts
    that raises -- distinct from a provider that simply doesn't override
    it at all (which honestly returns [], the OptionsDataProvider
    default, not an exception)."""

    def get_option_chain(
        self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
    ):
        return []

    def get_quotes_for_known_contracts(self, ticker, contracts, expiration, as_of, on_attempt=None):
        raise RuntimeError("simulated known-contracts quote failure")


class _RaisingSelectedLegsOptionsProvider(OptionsDataProvider):
    """Has a working option chain but a get_quotes_for_selected_legs
    that raises -- distinct from a provider that simply doesn't override
    it at all (which honestly delegates to get_option_chain, the
    OptionsDataProvider default, not an exception)."""

    def get_option_chain(
        self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
    ):
        return []

    def get_quotes_for_selected_legs(self, ticker, legs, expiration, as_of, on_attempt=None):
        raise RuntimeError("simulated selected-legs quote failure")


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


class TestOptionsProviderChainKnownContracts:
    """Phase 4.5 -- get_quotes_for_known_contracts must fall through the
    chain the same way get_option_chain does, so official benchmark exit
    capture (services/benchmark_exit_capture.py) gets a real live
    provider's quotes through the same primary-then-fallback
    configuration used everywhere else. Unlike get_underlying_quote, an
    empty result is a legitimate, honestly reported outcome (matching
    get_option_chain's own precedent) -- only a real exception falls
    through, and every provider failing raises AllProvidersFailedError,
    never a silent empty list."""

    _CONTRACTS = [
        KnownContract(strike=Decimal("100"), option_type="call", external_contract_id="12345")
    ]

    def test_uses_primary_when_it_works(self):
        chain = OptionsProviderChain(
            [("primary", _WorkingOptionsProvider()), ("fallback", _FailingOptionsProvider())]
        )
        quotes = chain.get_quotes_for_known_contracts(
            "NVDA", self._CONTRACTS, date(2026, 1, 16), datetime.now(UTC)
        )
        assert quotes[0].source_provider == "working"
        assert chain.last_actual_provider == "primary"
        assert chain.last_fallback_reason is None

    def test_falls_through_to_next_provider_on_failure(self):
        chain = OptionsProviderChain(
            [
                ("primary", _RaisingKnownContractsOptionsProvider()),
                ("fallback", _WorkingOptionsProvider()),
            ]
        )
        quotes = chain.get_quotes_for_known_contracts(
            "NVDA", self._CONTRACTS, date(2026, 1, 16), datetime.now(UTC)
        )
        assert quotes[0].source_provider == "working"
        assert chain.last_actual_provider == "fallback"
        assert "simulated known-contracts quote failure" in chain.last_fallback_reason

    def test_empty_result_from_primary_is_not_treated_as_a_failure(self):
        """A provider with no override (the ABC default, []) is a
        legitimate final answer, not a trigger to fall through -- mirrors
        get_option_chain's own "empty is a real result" precedent."""
        chain = OptionsProviderChain(
            [("primary", _FailingOptionsProvider()), ("fallback", _WorkingOptionsProvider())]
        )
        # _FailingOptionsProvider doesn't override get_quotes_for_known_
        # contracts, so it returns the ABC default ([]) rather than
        # raising -- the chain must accept that as primary's real answer.
        quotes = chain.get_quotes_for_known_contracts(
            "NVDA", self._CONTRACTS, date(2026, 1, 16), datetime.now(UTC)
        )
        assert quotes == []
        assert chain.last_actual_provider == "primary"

    def test_raises_when_all_providers_fail(self):
        chain = OptionsProviderChain(
            [
                ("primary", _RaisingKnownContractsOptionsProvider()),
                ("fallback", _RaisingKnownContractsOptionsProvider()),
            ]
        )
        with pytest.raises(AllProvidersFailedError):
            chain.get_quotes_for_known_contracts(
                "NVDA", self._CONTRACTS, date(2026, 1, 16), datetime.now(UTC)
            )


class TestOptionsProviderChainSelectedLegs:
    """IBKR execution-observability hardening (2026-08-26) -- without its
    own override, get_quotes_for_selected_legs would silently fall back
    to OptionsDataProvider's default (delegates to THIS chain's own
    get_option_chain), undoing Section 7's entry-capture efficiency fix
    the moment two options providers are ever configured. Same
    primary-then-fallback shape as TestOptionsProviderChainKnownContracts
    above."""

    _LEGS = [SelectedLeg(strike=Decimal("100"), option_type="call", action="buy")]

    def test_uses_primary_when_it_works(self):
        chain = OptionsProviderChain(
            [("primary", _WorkingOptionsProvider()), ("fallback", _FailingOptionsProvider())]
        )
        quotes = chain.get_quotes_for_selected_legs(
            "NVDA", self._LEGS, date(2026, 1, 16), datetime.now(UTC)
        )
        assert quotes[0].source_provider == "working"
        assert chain.last_actual_provider == "primary"
        assert chain.last_fallback_reason is None

    def test_falls_through_to_next_provider_on_failure(self):
        chain = OptionsProviderChain(
            [
                ("primary", _RaisingSelectedLegsOptionsProvider()),
                ("fallback", _WorkingOptionsProvider()),
            ]
        )
        quotes = chain.get_quotes_for_selected_legs(
            "NVDA", self._LEGS, date(2026, 1, 16), datetime.now(UTC)
        )
        assert quotes[0].source_provider == "working"
        assert chain.last_actual_provider == "fallback"
        assert "simulated selected-legs quote failure" in chain.last_fallback_reason

    def test_raises_when_all_providers_fail(self):
        chain = OptionsProviderChain(
            [
                ("primary", _RaisingSelectedLegsOptionsProvider()),
                ("fallback", _RaisingSelectedLegsOptionsProvider()),
            ]
        )
        with pytest.raises(AllProvidersFailedError):
            chain.get_quotes_for_selected_legs(
                "NVDA", self._LEGS, date(2026, 1, 16), datetime.now(UTC)
            )

    def test_on_attempt_forwarded_to_the_chosen_provider(self):
        """The chain must never swallow the telemetry hook -- a caller
        that wants per-attempt observability must still get it through
        the fallback layer, not just when calling a single provider
        directly."""
        received: list[str] = []

        class _ObservingProvider(OptionsDataProvider):
            def get_option_chain(
                self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
            ):
                return []

            def get_quotes_for_selected_legs(
                self, ticker, legs, expiration, as_of, on_attempt=None
            ):
                if on_attempt is not None:
                    received.append("called")
                return []

        chain = OptionsProviderChain([("primary", _ObservingProvider())])
        chain.get_quotes_for_selected_legs(
            "NVDA", self._LEGS, date(2026, 1, 16), datetime.now(UTC), on_attempt=lambda a: None
        )
        assert received == ["called"]


def _calendar_entry(source_provider: str) -> FinnhubCalendarEntry:
    return FinnhubCalendarEntry(
        symbol="NVDA",
        earnings_date=date(2026, 8, 26),
        session="amc",
        fiscal_year=None,
        fiscal_quarter=None,
        eps_estimate=Decimal("2.09"),
        revenue_estimate=Decimal("92072420560"),
        source_provider=source_provider,
        retrieved_at=datetime.now(UTC),
    )


def _company_profile(source_provider: str) -> FinnhubCompanyProfile:
    return FinnhubCompanyProfile(
        symbol="NVDA",
        name="NVIDIA Corporation",
        logo_url=None,
        exchange="NASDAQ",
        country="US",
        market_cap_millions=Decimal("5049594.08"),
        currency=None,
        source_provider=source_provider,
        retrieved_at=datetime.now(UTC),
    )


class _FailingCalendarProvider(EarningsCalendarProvider):
    def get_earnings_calendar(self, from_date, to_date):
        raise RuntimeError("simulated calendar provider failure")

    def get_company_profile(self, symbol):
        raise RuntimeError("simulated profile provider failure")


class _WorkingCalendarProvider(EarningsCalendarProvider):
    def __init__(self, source_provider: str = "working") -> None:
        self._source_provider = source_provider

    def get_earnings_calendar(self, from_date, to_date):
        return [_calendar_entry(self._source_provider)]

    def get_company_profile(self, symbol):
        return _company_profile(self._source_provider)


class _UnknownSymbolCalendarProvider(EarningsCalendarProvider):
    """Has a working calendar but honestly returns None for
    get_company_profile -- distinct from a provider that raises (the
    OptionsProviderChain.get_underlying_quote precedent this mirrors:
    both an exception and a None must fall through)."""

    def get_earnings_calendar(self, from_date, to_date):
        return [_calendar_entry("unknown-symbol")]

    def get_company_profile(self, symbol):
        return None


class TestEarningsCalendarProviderChain:
    def test_requires_at_least_one_provider(self):
        with pytest.raises(ValueError):
            EarningsCalendarProviderChain([])

    def test_uses_primary_when_it_works(self):
        chain = EarningsCalendarProviderChain(
            [
                ("earningsapi", _WorkingCalendarProvider("earningsapi")),
                ("finnhub", _FailingCalendarProvider()),
            ]
        )
        entries = chain.get_earnings_calendar(date(2026, 8, 26), date(2026, 8, 26))
        assert entries[0].source_provider == "earningsapi"
        assert chain.last_requested_provider == "earningsapi"
        assert chain.last_actual_provider == "earningsapi"
        assert chain.last_fallback_reason is None

    def test_falls_through_to_finnhub_on_earningsapi_failure(self):
        chain = EarningsCalendarProviderChain(
            [
                ("earningsapi", _FailingCalendarProvider()),
                ("finnhub", _WorkingCalendarProvider("finnhub")),
            ]
        )
        entries = chain.get_earnings_calendar(date(2026, 8, 26), date(2026, 8, 26))
        assert entries[0].source_provider == "finnhub"
        assert chain.last_requested_provider == "earningsapi"
        assert chain.last_actual_provider == "finnhub"
        assert "simulated calendar provider failure" in chain.last_fallback_reason

    def test_raises_when_all_providers_fail(self):
        chain = EarningsCalendarProviderChain(
            [("earningsapi", _FailingCalendarProvider()), ("finnhub", _FailingCalendarProvider())]
        )
        with pytest.raises(AllProvidersFailedError):
            chain.get_earnings_calendar(date(2026, 8, 26), date(2026, 8, 26))


class TestEarningsCalendarProviderChainCompanyProfile:
    def test_uses_primary_when_it_works(self):
        chain = EarningsCalendarProviderChain(
            [
                ("earningsapi", _WorkingCalendarProvider("earningsapi")),
                ("finnhub", _FailingCalendarProvider()),
            ]
        )
        profile = chain.get_company_profile("NVDA")
        assert profile is not None
        assert profile.source_provider == "earningsapi"
        assert chain.last_actual_provider == "earningsapi"
        assert chain.last_fallback_reason is None

    def test_falls_through_on_exception(self):
        chain = EarningsCalendarProviderChain(
            [
                ("earningsapi", _FailingCalendarProvider()),
                ("finnhub", _WorkingCalendarProvider("finnhub")),
            ]
        )
        profile = chain.get_company_profile("NVDA")
        assert profile is not None
        assert profile.source_provider == "finnhub"
        assert chain.last_actual_provider == "finnhub"
        assert "simulated profile provider failure" in chain.last_fallback_reason

    def test_falls_through_on_none(self):
        """An unknown-symbol None from the primary is not an exception --
        it's an honest "this provider doesn't know this symbol" -- but
        must still fall through to the fallback exactly like an
        exception would, since the fallback provider might know it."""
        chain = EarningsCalendarProviderChain(
            [
                ("earningsapi", _UnknownSymbolCalendarProvider()),
                ("finnhub", _WorkingCalendarProvider("finnhub")),
            ]
        )
        profile = chain.get_company_profile("NVDA")
        assert profile is not None
        assert profile.source_provider == "finnhub"
        assert chain.last_actual_provider == "finnhub"

    def test_returns_none_when_every_provider_lacks_the_symbol(self):
        chain = EarningsCalendarProviderChain(
            [
                ("earningsapi", _UnknownSymbolCalendarProvider()),
                ("finnhub", _UnknownSymbolCalendarProvider()),
            ]
        )
        profile = chain.get_company_profile("ZZUNKNOWN")
        assert profile is None
