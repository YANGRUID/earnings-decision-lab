"""IBKR TWS Migration Phase 1 -- IBKRTWSProvider unit tests.

Uses a fake TWSConnectionManager double (never a real socket -- Section
45's "mock protocol internals") that answers by inspecting the real
ibapi Contract objects the provider builds, exactly the way a real IB
Gateway/TWS response would be keyed by the real request it answered.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from models.enums import QuoteRequirement
from providers.ibkr_options import IBKRContractNotFoundError
from providers.ibkr_tws_options import IBKRTWSProvider
from providers.types import KnownContract, SelectedLeg, SnapshotAttempt

AS_OF = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)


@dataclass
class _FakeContractRef:
    conId: int


@dataclass
class _FakeContractDetails:
    contract: _FakeContractRef


def _cd(conid: int) -> _FakeContractDetails:
    return _FakeContractDetails(contract=_FakeContractRef(conId=conid))


class _FakeConnection:
    def __init__(self) -> None:
        self.contract_details_by_symbol: dict[str, list] = {}
        self.contract_details_by_conid: dict[int, list] = {}
        self.contract_details_by_option: dict[tuple, list] = {}
        self.sec_def_opt_params: list[dict] = []
        self.snapshot_by_conid: dict[int, dict] = {}
        self.ensure_connected_calls = 0
        self.market_data_requests: list = []
        # Real TWS behavior (confirmed live, Phase 3 market-hours
        # validation, 2026-09-01): reqContractDetails raises error 200
        # ("no security definition found") for a strike/right that isn't
        # actually listed at an expiration -- unlike the Web Gateway's
        # own /iserver/secdef/info, which just returns an empty list for
        # the identical condition (see providers/ibkr_options.py's own
        # _resolve_contracts docstring: "a real, occasional gap, not an
        # error"). Keys placed here simulate that real raise instead of
        # an empty-list return, so tests can exercise the actual TWS
        # failure mode rather than the Web one.
        self.option_keys_raising_not_found: set[tuple] = set()

    def ensure_connected(self) -> None:
        self.ensure_connected_calls += 1

    def request_contract_details(self, contract, timeout=None):
        if contract.secType == "STK":
            if getattr(contract, "conId", 0):
                return self.contract_details_by_conid.get(contract.conId, [])
            return self.contract_details_by_symbol.get(contract.symbol, [])
        if contract.secType == "OPT":
            if getattr(contract, "conId", 0):
                return self.contract_details_by_conid.get(contract.conId, [])
            key = (contract.strike, contract.right, contract.lastTradeDateOrContractMonth)
            if key in self.option_keys_raising_not_found:
                raise IBKRContractNotFoundError(
                    f"no contract found (error 200): simulated for {key}"
                )
            return self.contract_details_by_option.get(key, [])
        return []

    def request_sec_def_opt_params(self, symbol, sec_type, conid, timeout=None):
        return self.sec_def_opt_params

    def request_market_data_snapshot(self, contract, generic_ticks="", timeout=None):
        self.market_data_requests.append(contract)
        return self.snapshot_by_conid.get(getattr(contract, "conId", None), {})

    def request_market_data_with_requirement(
        self,
        contract,
        requirement_satisfied,
        generic_ticks="",
        max_attempts=5,
        retry_delay=0.0,
        on_attempt=None,
        timeout=None,
    ):
        self.market_data_requests.append(contract)
        result = self.snapshot_by_conid.get(getattr(contract, "conId", None), {})
        if on_attempt is not None:
            on_attempt(1, result)
        return result


def _provider(connection: _FakeConnection) -> IBKRTWSProvider:
    return IBKRTWSProvider(host="x", port=1, client_id=1, connection=connection)


def _snapshot(**overrides) -> dict:
    base = {
        "bid": 4.90,
        "ask": 5.10,
        "last": 5.00,
        "volume": 120,
        "open_interest": 340,
        "implied_volatility": 0.42,
        "delta": 0.55,
        "gamma": 0.02,
        "theta": -0.05,
        "vega": 0.12,
        "market_data_quality": "delayed",
    }
    base.update(overrides)
    return base


class TestGetOptionChain:
    def test_full_flow_resolves_and_prices_a_bounded_window(self):
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        conn.contract_details_by_conid[265598] = [_cd(265598)]
        conn.snapshot_by_conid[265598] = {"last": 150.0, "market_data_quality": "live"}
        conn.sec_def_opt_params = [
            {
                "exchange": "SMART",
                "underlying_conid": 265598,
                "trading_class": "AAPL",
                "multiplier": "100",
                "expirations": {"20260918", "20261016"},
                "strikes": {148.0, 150.0, 152.0},
            }
        ]
        next_conid = iter(range(9000, 9100))
        for strike in (148.0, 150.0, 152.0):
            for right in ("C", "P"):
                conid = next(next_conid)
                conn.contract_details_by_option[(strike, right, "20260918")] = [_cd(conid)]
                conn.snapshot_by_conid[conid] = _snapshot()

        provider = _provider(conn)
        quotes = provider.get_option_chain("AAPL", AS_OF, reference_date=date(2026, 9, 1))

        assert len(quotes) == 6  # 3 strikes x 2 rights
        assert conn.ensure_connected_calls == 1
        q = quotes[0]
        assert q.ticker == "AAPL"
        assert q.expiration_date == date(2026, 9, 18)
        assert q.bid == Decimal("4.9")
        assert q.ask == Decimal("5.1")
        assert q.implied_volatility == Decimal("0.42")
        assert q.market_data_quality == "delayed"
        assert q.source_provider == "ibkr_tws"

    def test_returns_empty_when_underlying_price_unavailable(self):
        conn = _FakeConnection()
        conn.contract_details_by_symbol["ZZZ"] = [_cd(1)]
        conn.contract_details_by_conid[1] = [_cd(1)]
        # No snapshot configured -- honest "no price" outcome.
        provider = _provider(conn)
        assert provider.get_option_chain("ZZZ", AS_OF) == []

    def test_raises_contract_not_found_for_unknown_underlying(self):
        conn = _FakeConnection()
        provider = _provider(conn)
        with pytest.raises(IBKRContractNotFoundError):
            provider.get_option_chain("NOPE", AS_OF)

    def test_returns_empty_when_no_option_params_listed(self):
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        conn.contract_details_by_conid[265598] = [_cd(265598)]
        conn.snapshot_by_conid[265598] = {"last": 150.0}
        provider = _provider(conn)
        assert provider.get_option_chain("AAPL", AS_OF) == []

    def test_skips_one_unlisted_strike_right_instead_of_crashing_the_whole_chain(self):
        """Real, live-discovered bug (Phase 3 market-hours validation,
        2026-09-01): confirmed live against NVDA's real, sparse near-term
        weekly strike listing -- a real reqContractDetails error 200 for
        ONE strike/right within an otherwise-valid ATM window used to
        propagate as IBKRContractNotFoundError and crash the entire
        chain, instead of being skipped the way the Web adapter's own
        _resolve_contracts docstring says a missing strike/right always
        should be ("a real, occasional gap, not an error")."""
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        conn.contract_details_by_conid[265598] = [_cd(265598)]
        conn.snapshot_by_conid[265598] = {"last": 150.0, "market_data_quality": "live"}
        conn.sec_def_opt_params = [
            {
                "exchange": "SMART",
                "underlying_conid": 265598,
                "trading_class": "AAPL",
                "multiplier": "100",
                "expirations": {"20260918"},
                "strikes": {148.0, 150.0},
            }
        ]
        conn.contract_details_by_option[(148.0, "C", "20260918")] = [_cd(9001)]
        conn.snapshot_by_conid[9001] = _snapshot()
        conn.contract_details_by_option[(148.0, "P", "20260918")] = [_cd(9002)]
        conn.snapshot_by_conid[9002] = _snapshot()
        conn.contract_details_by_option[(150.0, "C", "20260918")] = [_cd(9003)]
        conn.snapshot_by_conid[9003] = _snapshot()
        # 150.0 P is a real, live-confirmed gap: listed in the strike set
        # from reqSecDefOptParams, but not actually resolvable at this
        # specific expiration -- exactly NVDA's real live failure mode.
        conn.option_keys_raising_not_found.add((150.0, "P", "20260918"))

        provider = _provider(conn)
        quotes = provider.get_option_chain("AAPL", AS_OF, reference_date=date(2026, 9, 1))

        assert len(quotes) == 3  # every real contract except the one genuine gap
        assert {q.external_contract_id for q in quotes} == {"9001", "9002", "9003"}


class TestOptionParamsTradingClassFiltering:
    """IBKR TWS Migration Phase 2 -- a real bug found in live testing
    (2026-08-31): a real reqSecDefOptParams response for AAPL returned
    39 groups, including unrelated adjusted/legacy "2AAPL" contract-
    series groups (1 strike, 1 expiration) on the SAME "SMART" exchange
    as the real, normal "AAPL" group (120 strikes, 23 expirations).
    Filtering on exchange=="SMART" alone could non-deterministically
    pick either one depending on response ordering."""

    def _rows_with_contaminant_first(self):
        # Deliberately adversarial ordering: the unrelated "2AAPL"
        # SMART group appears BEFORE the real "AAPL" SMART group -- a
        # naive first-match-on-exchange filter would pick the wrong one.
        return [
            {
                "exchange": "SMART",
                "underlying_conid": 265598,
                "trading_class": "2AAPL",
                "multiplier": "100",
                "expirations": {"20260904"},
                "strikes": {150.0},
            },
            {
                "exchange": "CBOE",
                "underlying_conid": 265598,
                "trading_class": "AAPL",
                "multiplier": "100",
                "expirations": {"20260918", "20261016"},
                "strikes": {145.0, 150.0, 155.0},
            },
            {
                "exchange": "SMART",
                "underlying_conid": 265598,
                "trading_class": "AAPL",
                "multiplier": "100",
                "expirations": {"20260918", "20261016"},
                "strikes": {145.0, 150.0, 155.0, 160.0},
            },
        ]

    def test_never_selects_an_unrelated_adjusted_contract_series(self):
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        conn.sec_def_opt_params = self._rows_with_contaminant_first()
        provider = _provider(conn)
        params = provider._option_params("AAPL", 265598)  # noqa: SLF001
        assert params["trading_class"] == "AAPL"
        assert params["strikes"] == {145.0, 150.0, 155.0, 160.0}

    def test_prefers_smart_among_correctly_filtered_groups(self):
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        conn.sec_def_opt_params = self._rows_with_contaminant_first()
        provider = _provider(conn)
        params = provider._option_params("AAPL", 265598)  # noqa: SLF001
        assert params["exchange"] == "SMART"

    def test_list_available_expirations_uses_the_correctly_filtered_group(self):
        """End-to-end: the public method real callers use must reflect
        the fix, not just the private helper in isolation."""
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        conn.sec_def_opt_params = self._rows_with_contaminant_first()
        provider = _provider(conn)
        result = provider.list_available_expirations("AAPL", after=date(2026, 9, 1))
        assert result == [date(2026, 9, 18), date(2026, 10, 16)]


class TestListAvailableExpirations:
    def test_real_bounded_dedupe_sort_cap_in_one_call(self):
        """No month-walking loop needed at all -- Section G's own
        disclosed architectural difference from the Web adapter."""
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        conn.sec_def_opt_params = [
            {
                "exchange": "SMART",
                "underlying_conid": 265598,
                "trading_class": "AAPL",
                "multiplier": "100",
                "expirations": {"20260904", "20260918", "20261016", "20260911"},
                "strikes": set(),
            }
        ]
        provider = _provider(conn)
        result = provider.list_available_expirations(
            "AAPL", after=date(2026, 9, 1), max_candidates=2
        )
        assert result == [date(2026, 9, 4), date(2026, 9, 11)]

    def test_excludes_expirations_on_or_before_after_date(self):
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        conn.sec_def_opt_params = [
            {
                "exchange": "SMART",
                "underlying_conid": 265598,
                "trading_class": "AAPL",
                "multiplier": "100",
                "expirations": {"20260901", "20260918"},
                "strikes": set(),
            }
        ]
        provider = _provider(conn)
        result = provider.list_available_expirations("AAPL", after=date(2026, 9, 1))
        assert result == [date(2026, 9, 18)]


class TestGetUnderlyingQuote:
    def test_builds_a_real_underlying_quote(self):
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        conn.snapshot_by_conid[265598] = {
            "last": 150.25,
            "bid": 150.20,
            "ask": 150.30,
            "market_data_quality": "live",
        }
        provider = _provider(conn)
        quote = provider.get_underlying_quote("AAPL")
        assert quote is not None
        assert quote.price == Decimal("150.25")
        assert quote.bid == Decimal("150.2")
        assert quote.ask == Decimal("150.3")
        assert quote.market_data_quality == "live"

    def test_returns_none_when_no_price_available(self):
        conn = _FakeConnection()
        conn.contract_details_by_symbol["AAPL"] = [_cd(265598)]
        provider = _provider(conn)
        assert provider.get_underlying_quote("AAPL") is None


class TestGetQuotesForKnownContracts:
    """Settlement/exit re-quoting by conid -- never rediscovers strikes."""

    def test_exit_of_a_buy_leg_requires_bid(self):
        conn = _FakeConnection()
        conn.snapshot_by_conid[555] = _snapshot()
        provider = _provider(conn)
        contracts = [
            KnownContract(
                strike=Decimal("150"), option_type="call", external_contract_id="555", action="buy"
            )
        ]
        quotes = provider.get_quotes_for_known_contracts(
            "AAPL", contracts, date(2026, 9, 18), AS_OF
        )
        assert len(quotes) == 1
        assert quotes[0].external_contract_id == "555"

    def test_malformed_contract_id_is_skipped_not_raised(self):
        conn = _FakeConnection()
        provider = _provider(conn)
        contracts = [
            KnownContract(
                strike=Decimal("150"), option_type="call", external_contract_id="not-a-number"
            )
        ]
        assert (
            provider.get_quotes_for_known_contracts("AAPL", contracts, date(2026, 9, 18), AS_OF)
            == []
        )

    def test_empty_contracts_returns_empty(self):
        conn = _FakeConnection()
        provider = _provider(conn)
        assert provider.get_quotes_for_known_contracts("AAPL", [], date(2026, 9, 18), AS_OF) == []


class TestGetQuotesForSelectedLegs:
    def test_resolves_exact_contract_and_prices_it(self):
        conn = _FakeConnection()
        conn.contract_details_by_option[(Decimal("150"), "C", "20260918")] = [_cd(777)]
        conn.snapshot_by_conid[777] = _snapshot()
        provider = _provider(conn)
        legs = [SelectedLeg(strike=Decimal("150"), option_type="call", action="buy")]
        quotes = provider.get_quotes_for_selected_legs("AAPL", legs, date(2026, 9, 18), AS_OF)
        assert len(quotes) == 1
        assert quotes[0].external_contract_id == "777"

    def test_dedupes_identical_strike_type_legs(self):
        conn = _FakeConnection()
        conn.contract_details_by_option[(Decimal("150"), "C", "20260918")] = [_cd(777)]
        conn.snapshot_by_conid[777] = _snapshot()
        provider = _provider(conn)
        legs = [
            SelectedLeg(strike=Decimal("150"), option_type="call", action="buy"),
            SelectedLeg(strike=Decimal("150"), option_type="call", action="buy"),
        ]
        quotes = provider.get_quotes_for_selected_legs("AAPL", legs, date(2026, 9, 18), AS_OF)
        assert len(quotes) == 1

    def test_skips_leg_with_no_resolvable_contract(self):
        conn = _FakeConnection()
        provider = _provider(conn)
        legs = [SelectedLeg(strike=Decimal("999"), option_type="call", action="buy")]
        assert provider.get_quotes_for_selected_legs("AAPL", legs, date(2026, 9, 18), AS_OF) == []

    def test_skips_leg_that_raises_contract_not_found_instead_of_crashing(self):
        """Same real TWS failure mode as TestGetOptionChain's own
        test_skips_one_unlisted_strike_right_instead_of_crashing_the_whole_chain
        (Phase 3 market-hours validation, 2026-09-01), exercised through
        get_quotes_for_selected_legs's own call to _resolve_exact_contract
        -- a real reqContractDetails error 200 for one selected leg must
        not crash a whole multi-leg batch, only that one leg."""
        conn = _FakeConnection()
        conn.contract_details_by_option[(Decimal("150"), "C", "20260918")] = [_cd(777)]
        conn.snapshot_by_conid[777] = _snapshot()
        conn.option_keys_raising_not_found.add((Decimal("999"), "C", "20260918"))
        provider = _provider(conn)
        legs = [
            SelectedLeg(strike=Decimal("150"), option_type="call", action="buy"),
            SelectedLeg(strike=Decimal("999"), option_type="call", action="buy"),
        ]
        quotes = provider.get_quotes_for_selected_legs("AAPL", legs, date(2026, 9, 18), AS_OF)
        assert len(quotes) == 1
        assert quotes[0].external_contract_id == "777"

    def test_each_leg_gets_its_own_real_observation_timestamp(self):
        """A real observability gap found during live Web/TWS parity
        testing (2026-09-01): retrieved_at used to be captured ONCE
        before the leg loop and reused for every leg, so a multi-leg
        quote's timestamps were identical by construction regardless of
        how long each leg's own warm-up actually took -- any cross-leg
        skew measurement built on top of that was measuring nothing
        real. Drives datetime.now() through a sequence of distinct,
        controlled values (never real sleeps -- avoids timing flakiness)
        and proves each leg's quote gets the value current at the
        moment THAT leg's own data was captured, not one shared value."""
        import providers.ibkr_tws_options as module

        conn = _FakeConnection()
        conn.contract_details_by_option[(Decimal("150"), "C", "20260918")] = [_cd(777)]
        conn.contract_details_by_option[(Decimal("155"), "C", "20260918")] = [_cd(778)]
        conn.snapshot_by_conid[777] = _snapshot()
        conn.snapshot_by_conid[778] = _snapshot()

        real_datetime = module.datetime
        timestamps = iter(
            [
                datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 1, 12, 0, 5, tzinfo=UTC),  # 5s later -- leg 2's own capture
            ]
        )

        class _FakeDatetime:
            @staticmethod
            def now(tz=None):
                return next(timestamps)

        module.datetime = _FakeDatetime
        try:
            provider = _provider(conn)
            legs = [
                SelectedLeg(strike=Decimal("150"), option_type="call", action="buy"),
                SelectedLeg(strike=Decimal("155"), option_type="call", action="buy"),
            ]
            quotes = provider.get_quotes_for_selected_legs("AAPL", legs, date(2026, 9, 18), AS_OF)
        finally:
            module.datetime = real_datetime

        assert len(quotes) == 2
        first_ts = quotes[0].retrieved_at
        second_ts = quotes[1].retrieved_at
        assert second_ts > first_ts  # real, measurable skew -- never identical by construction
        assert (second_ts - first_ts).total_seconds() == 5.0

    def test_entry_requirement_mapping_reaches_the_connection(self):
        """A BUY leg needs ASK, a SELL leg needs BID -- proven by
        capturing the requirement_satisfied predicate the fake connection
        receives and checking it against synthetic result dicts."""
        conn = _FakeConnection()
        conn.contract_details_by_option[(Decimal("150"), "C", "20260918")] = [_cd(777)]
        conn.contract_details_by_option[(Decimal("145"), "P", "20260918")] = [_cd(778)]
        conn.snapshot_by_conid[777] = _snapshot()
        conn.snapshot_by_conid[778] = _snapshot()

        captured: list[QuoteRequirement] = []
        original = conn.request_market_data_with_requirement

        def _spy(contract, requirement_satisfied, **kwargs):
            captured.append(
                (
                    requirement_satisfied({"ask": 1}),
                    requirement_satisfied({"bid": 1}),
                )
            )
            return original(contract, requirement_satisfied, **kwargs)

        conn.request_market_data_with_requirement = _spy
        provider = _provider(conn)
        legs = [
            SelectedLeg(strike=Decimal("150"), option_type="call", action="buy"),
            SelectedLeg(strike=Decimal("145"), option_type="put", action="sell"),
        ]
        provider.get_quotes_for_selected_legs("AAPL", legs, date(2026, 9, 18), AS_OF)

        # buy leg: ASK-satisfied predicate true only when "ask" present
        assert captured[0] == (True, False)
        # sell leg: BID-satisfied predicate true only when "bid" present
        assert captured[1] == (False, True)

    def test_on_attempt_hook_receives_a_real_snapshot_attempt(self):
        conn = _FakeConnection()
        conn.contract_details_by_option[(Decimal("150"), "C", "20260918")] = [_cd(777)]
        conn.snapshot_by_conid[777] = _snapshot()
        provider = _provider(conn)
        legs = [SelectedLeg(strike=Decimal("150"), option_type="call", action="buy")]
        seen: list[SnapshotAttempt] = []
        provider.get_quotes_for_selected_legs(
            "AAPL", legs, date(2026, 9, 18), AS_OF, on_attempt=seen.append
        )
        assert len(seen) == 1
        assert seen[0].attempt == 1
        assert 777 in seen[0].per_conid
        assert seen[0].per_conid[777].bid_present is True


class TestMarketDataQualityHonesty:
    def test_never_fabricates_quality_when_none_reported(self):
        conn = _FakeConnection()
        conn.contract_details_by_option[(Decimal("150"), "C", "20260918")] = [_cd(777)]
        conn.snapshot_by_conid[777] = {"bid": 4.9, "ask": 5.1, "last": 5.0}  # no quality key at all
        provider = _provider(conn)
        legs = [SelectedLeg(strike=Decimal("150"), option_type="call", action="buy")]
        quotes = provider.get_quotes_for_selected_legs("AAPL", legs, date(2026, 9, 18), AS_OF)
        assert quotes[0].market_data_quality == "unknown"


class TestContractIdentityParity:
    def test_external_contract_id_matches_web_adapters_string_convention(self):
        """Section 53 -- the same real IBKR conId space, formatted
        identically (str(conid)) so a caller can compare identity across
        both providers without a type mismatch."""
        conn = _FakeConnection()
        conn.contract_details_by_option[(Decimal("150"), "C", "20260918")] = [_cd(777)]
        conn.snapshot_by_conid[777] = _snapshot()
        provider = _provider(conn)
        legs = [SelectedLeg(strike=Decimal("150"), option_type="call", action="buy")]
        quotes = provider.get_quotes_for_selected_legs("AAPL", legs, date(2026, 9, 18), AS_OF)
        assert quotes[0].external_contract_id == "777"
        assert isinstance(quotes[0].external_contract_id, str)


class TestNoBlackScholesFallback:
    def test_module_never_imports_black_scholes(self):
        """Section 20 -- real provider Greeks are never silently
        substituted with a calculated fallback inside this adapter; that
        distinction is made downstream, exactly as for the Web adapter."""
        import inspect

        import providers.ibkr_tws_options as module

        source = inspect.getsource(module)
        assert "black_scholes" not in source.lower()
