"""V4.2 -- bounded multi-expiry candidate construction.

Three properties matter here and nothing else does. The ladder stays bounded
no matter how many expirations are listed; each expiry derives its OWN
economics rather than inheriting the nearest expiry's; and the extra choice
does not multiply market-data acquisition, because contracts are deduplicated
across expiries, strategies and variants before anything is quoted.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from providers.types import OptionQuote, UnderlyingQuote
from services.v4_2_multi_expiry import (
    MULTI_EXPIRY_OK,
    MULTI_EXPIRY_UNAVAILABLE,
    build_multi_expiry_universe,
    summarize_multi_expiry,
)

D = Decimal
AS_OF = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
EARNINGS = date(2026, 9, 10)
SETTLEMENT = date(2026, 9, 11)

# Deliberately different ATM straddle prices per expiry: a nearer expiry is
# cheaper, so a clone of the near-expiry implied move into the later ones is
# immediately visible as identical numbers where they should differ.
STRADDLE_BY_EXPIRY = {
    date(2026, 9, 11): D("1.00"),
    date(2026, 9, 18): D("2.50"),
    date(2026, 9, 25): D("4.00"),
    date(2026, 10, 16): D("6.00"),
}


class FakeChainProvider:
    """A chain with several listed expiries and a real per-expiry price
    surface. Counts every call so the request budget can be asserted."""

    def __init__(self, expirations, *, strikes=None, fail_for=()):
        self.expirations = list(expirations)
        self.strikes = strikes or [D(str(s)) for s in range(90, 111, 5)]
        self.fail_for = set(fail_for)
        self.metadata_calls = 0
        self.chain_calls: list[date] = []
        self.quote_calls: list[tuple[date, tuple]] = []
        self.underlying_calls = 0

    def get_underlying_quote(self, ticker):
        self.underlying_calls += 1
        return UnderlyingQuote(
            source_provider="fake",
            retrieved_at=AS_OF,
            ticker=ticker,
            price=D("100"),
            timestamp=AS_OF,
            market_data_quality="delayed",
        )

    def get_chain_metadata(self, ticker):
        self.metadata_calls += 1
        return {
            "underlying_conid": "1",
            "trading_class": ticker,
            "exchange": "SMART",
            "multiplier": "100",
            "expirations": list(self.expirations),
            "strikes": list(self.strikes),
            "source_provider": "fake",
        }

    def _quote(self, ticker, expiration, strike, right):
        straddle = STRADDLE_BY_EXPIRY.get(expiration, D("3.00"))
        # A crude but monotonic surface: ATM costs the straddle half, and
        # value decays away from the money. Real enough to construct spreads.
        distance = abs(strike - D("100"))
        mid = max(straddle / 2 - distance / D("20"), D("0.05"))
        return OptionQuote(
            source_provider="fake",
            retrieved_at=AS_OF,
            ticker=ticker,
            snapshot_timestamp=AS_OF,
            expiration_date=expiration,
            strike=strike,
            option_type=right,
            bid=mid - D("0.05"),
            ask=mid + D("0.05"),
            last_price=mid,
            implied_volatility=D("0.45"),
            delta=D("0.5"),
            volume=100,
            open_interest=500,
            bid_size=10,
            ask_size=10,
            market_data_quality="delayed",
            external_contract_id=f"{expiration.isoformat()}-{strike}-{right}",
        )

    def get_option_chain(self, ticker, as_of, expiration=None):
        if expiration in self.fail_for:
            raise RuntimeError("chain unavailable for this expiry")
        self.chain_calls.append(expiration)
        return [
            self._quote(ticker, expiration, strike, right)
            for strike in self.strikes
            for right in ("call", "put")
        ]

    def get_quotes_for_selected_legs(self, ticker, legs, expiration, as_of):
        self.quote_calls.append(
            (expiration, tuple(sorted((str(leg.strike), leg.option_type) for leg in legs)))
        )
        return [self._quote(ticker, expiration, leg.strike, leg.option_type) for leg in legs]


def _build(provider, **kwargs):
    return build_multi_expiry_universe(
        provider=provider,
        ticker="MEXP",
        as_of=AS_OF,
        direction="neutral",
        volatility_view=None,
        earnings_date=EARNINGS,
        settlement_date=SETTLEMENT,
        **kwargs,
    )


class TestBoundedLadder:
    def test_a_single_listed_expiry_produces_one_rung(self):
        provider = FakeChainProvider([date(2026, 9, 18)])
        result = _build(provider)
        assert len(result.ladder) == 1
        assert result.expiries_considered == 1

    def test_two_listed_expiries_produce_two_rungs(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 18)])
        result = _build(provider)
        assert len(result.ladder) == 2

    def test_three_listed_expiries_produce_three_rungs(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25)])
        result = _build(provider)
        assert len(result.ladder) == 3
        assert [v.ladder_position for v in result.ladder] == [0, 1, 2]

    def test_more_than_three_listed_expiries_stay_bounded_to_three(self):
        provider = FakeChainProvider(
            [
                date(2026, 9, 11),
                date(2026, 9, 18),
                date(2026, 9, 25),
                date(2026, 10, 16),
                date(2026, 11, 20),
                date(2026, 12, 18),
            ]
        )
        result = _build(provider)
        assert len(result.ladder) == 3, "the bound is the point; six listed is not six rungs"
        assert len(provider.chain_calls) == 3
        assert max(v.expiration for v in result.ladder) == date(2026, 9, 25)

    def test_an_expiry_on_or_before_the_earnings_date_is_never_eligible(self):
        provider = FakeChainProvider([date(2026, 9, 4), EARNINGS, date(2026, 9, 18)])
        result = _build(provider)
        assert [v.expiration for v in result.ladder] == [date(2026, 9, 18)]

    def test_no_minimum_dte_rule_excludes_a_same_day_expiry(self):
        """A contract expiring ON the settlement date stays eligible: the
        audit's conclusion was to compare expiries on economics, not to
        legislate a minimum remaining life."""
        provider = FakeChainProvider([SETTLEMENT, date(2026, 9, 18)])
        result = _build(provider)
        rung0 = result.ladder[0]
        assert rung0.expiration == SETTLEMENT
        assert rung0.dte_at_settlement == 0
        assert rung0.settlement_risk == "EXPIRES_ON_SETTLEMENT_DATE"
        assert result.per_expiry[0].candidates, "still constructed, not banned"


class TestExpirySpecificEconomics:
    def test_each_expiry_derives_its_own_implied_move(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25)])
        result = _build(provider)
        implied = [s.implied_move_pct for s in result.per_expiry]
        assert all(v is not None for v in implied)
        assert len(set(implied)) == 3, (
            "three expiries with three different straddles must not share one "
            "implied move -- that would be the nearest expiry's number cloned"
        )
        assert implied[0] < implied[1] < implied[2]

    def test_candidates_carry_their_own_expiry_context(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 25)])
        result = _build(provider)
        contexts = result.expiry_context_by_candidate
        assert contexts
        by_expiration: dict[str, set] = {}
        for context in contexts.values():
            by_expiration.setdefault(context["expiration"], set()).add(context["implied_move_pct"])
        assert len(by_expiration) == 2
        for values in by_expiration.values():
            assert len(values) == 1, "one implied move per expiry, shared within it"
        distinct = {next(iter(values)) for values in by_expiration.values()}
        assert len(distinct) == 2, "and the two expiries do not share the same one"

    def test_dte_is_expiry_specific(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 25)])
        result = _build(provider)
        assert [v.entry_dte for v in result.ladder] == [1, 15]
        assert [v.dte_at_settlement for v in result.ladder] == [0, 14]

    def test_a_candidate_id_is_unique_per_expiry(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 25)])
        result = _build(provider)
        ids = [c.candidate_id for c in result.candidates]
        assert len(ids) == len(set(ids)), "the same geometry on two expiries is two candidates"
        assert any("@2026-09-11" in cid for cid in ids)
        assert any("@2026-09-25" in cid for cid in ids)

    def test_every_candidate_is_valued_at_the_same_t1_objective(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 25)])
        result = _build(provider)
        exits = {c.context.expected_exit_timestamp for c in result.candidates}
        assert exits == {AS_OF}, "one T+1 objective, never each expiry's own payoff"


class TestRequestBudget:
    def test_contracts_are_deduplicated_before_any_quote(self):
        provider = FakeChainProvider([date(2026, 9, 18)])
        result = _build(provider)
        assert result.budget.contracts_deduplicated > 0, (
            "many candidates share strikes; the dedupe must actually save requests"
        )
        _expiration, contracts = provider.quote_calls[0]
        assert len(contracts) == len(set(contracts))
        assert len(contracts) == result.budget.unique_contracts_quoted

    def test_one_quote_call_per_expiration_not_per_candidate(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25)])
        result = _build(provider)
        assert len(provider.quote_calls) == 3
        assert len(result.candidates) > 3, "many candidates, three quote calls"

    def test_the_underlying_and_metadata_are_fetched_once_for_the_whole_ladder(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25)])
        _build(provider)
        assert provider.underlying_calls == 1
        assert provider.metadata_calls == 1

    def test_no_chain_wide_sweep(self):
        """Quotes are requested for the exact candidate legs, never for every
        listed strike."""
        provider = FakeChainProvider(
            [date(2026, 9, 18)],
            strikes=[D(str(s)) for s in range(50, 151, 1)],
        )
        result = _build(provider)
        _expiration, contracts = provider.quote_calls[0]
        assert len(contracts) < 40, f"quoted {len(contracts)} contracts of 202 listed"
        assert result.budget.unique_contracts_quoted == len(contracts)


class TestFailureIsolation:
    def test_one_failing_expiry_does_not_lose_the_others(self):
        provider = FakeChainProvider(
            [date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25)],
            fail_for=[date(2026, 9, 18)],
        )
        result = _build(provider)
        assert result.status == MULTI_EXPIRY_OK
        assert result.expiries_considered == 2
        failed = [s for s in result.per_expiry if s.failure_category]
        assert len(failed) == 1
        assert failed[0].variant.expiration == date(2026, 9, 18)

    def test_no_listed_expiration_is_reported_not_invented(self):
        provider = FakeChainProvider([])
        result = _build(provider)
        assert result.status == MULTI_EXPIRY_UNAVAILABLE
        assert result.failure_category == "CHAIN_METADATA_FAILED"
        assert result.candidates == []

    def test_no_eligible_expiration_is_reported_honestly(self):
        provider = FakeChainProvider([date(2026, 9, 4)])
        result = _build(provider)
        assert result.failure_category == "NO_ELIGIBLE_EXPIRATION"
        assert result.candidates == []

    def test_a_provider_that_raises_never_propagates(self):
        class Broken(FakeChainProvider):
            def get_underlying_quote(self, ticker):
                raise RuntimeError("TWS is down")

        result = _build(Broken([date(2026, 9, 18)]))
        assert result.failure_category == "MARKET_DATA_UNAVAILABLE"
        assert "TWS is down" in (result.failure_detail or "")


class TestSummary:
    def test_the_summary_reports_per_expiry_without_declaring_a_winner(self):
        provider = FakeChainProvider([date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25)])
        summary = summarize_multi_expiry(_build(provider))
        assert summary["expiries_considered"] == 3
        assert len(summary["per_expiry"]) == 3
        assert "winner" not in summary
        assert "best_expiry" not in summary
        for rung in summary["per_expiry"]:
            assert rung["implied_move_source"] in {"atm_straddle", "unavailable"}
        assert summary["budget"]["total_requests"] == 1 + 1 + 3 + 3

    @pytest.mark.parametrize("max_variants", [1, 2, 3])
    def test_the_bound_is_configurable_but_still_a_bound(self, max_variants):
        provider = FakeChainProvider(
            [date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 16)]
        )
        result = _build(provider, max_variants=max_variants)
        assert len(result.ladder) == max_variants
        assert len(provider.chain_calls) == max_variants
