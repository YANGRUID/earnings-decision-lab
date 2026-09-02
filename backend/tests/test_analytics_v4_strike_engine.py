"""V4.3 expected-move-aware strike-selection engine tests (2026-09-02).
Covers per-strategy construction (Sections 8-14), synthetic-chain
sanity checks (Section 25), scale invariance (Section 26), chain
granularity (Section 27), ordering invariants (Section 28), symmetry
(Section 29), and honest UNCONSTRUCTABLE failure (Section 21). Every
case was hand-verified against a manual synthetic-chain run before
being written here; none were fitted against the 7 real settled
trades (Section 27's anti-fitting rule, mirrored from V4.2)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_expected_move import ExpectedMoveContext, derive_expected_move_context
from analytics.decision.v4_market_coherence import MarketCoherenceResult
from analytics.decision.v4_strike_engine import (
    CHAIN_GRANULARITY_MINIMUM_WING,
    MARKET_COHERENCE_NOT_FRESH,
    UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED,
    UNCONSTRUCTABLE_MISSING_LEG_QUOTE,
    UNCONSTRUCTABLE_NO_PROTECTIVE_WING_AVAILABLE,
    UNCONSTRUCTABLE_SHARED_CENTER_MISMATCH,
    select_v4_strikes,
)
from analytics.options.strategy_candidates import StrategyCategory
from providers.types import OptionQuote

EXP = date(2026, 9, 18)
NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _quote(strike: Decimal, option_type: str, bid: Decimal, ask: Decimal) -> OptionQuote:
    return OptionQuote(
        ticker="ZZ",
        snapshot_timestamp=NOW,
        expiration_date=EXP,
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        volume=100,
        open_interest=500,
        source_provider="test",
        retrieved_at=NOW,
    )


def _price(spot: Decimal, strike: Decimal) -> Decimal:
    """Percentage-of-spot extrinsic decay -- deliberately scale-free so
    the same formula produces proportionally identical chains at any
    price level (needed for the scale-invariance tests below)."""
    distance_pct = abs(strike - spot) / spot
    extrinsic_pct = max(Decimal("0.0005"), Decimal("0.05") - distance_pct * Decimal("0.3"))
    return (spot * extrinsic_pct).quantize(Decimal("0.01"))


def _chain(spot: Decimal, strikes: list[Decimal]) -> list[OptionQuote]:
    quotes = []
    for k in strikes:
        mid = _price(spot, k)
        for right in ("call", "put"):
            quotes.append(_quote(k, right, mid - Decimal("0.05"), mid + Decimal("0.05")))
    return quotes


def _grid(spot: Decimal, step: Decimal, count_each_side: int) -> list[Decimal]:
    return [spot + step * Decimal(i) for i in range(-count_each_side, count_each_side + 1)]


def _context(
    spot: Decimal,
    quotes: list[OptionQuote] | None,
    historical_moves: list[Decimal] | None = None,
) -> ExpectedMoveContext:
    return derive_expected_move_context(
        spot=spot,
        observed_at=NOW,
        expiration=EXP,
        quotes_for_expiration=quotes,
        historical_next_day_move_pcts=historical_moves,
    )


SPOT = Decimal("100")
WIDE_STRIKES = _grid(SPOT, Decimal("5"), 4)  # 80..120
WIDE_CHAIN = _chain(SPOT, WIDE_STRIKES)
# Median move = 5% -> historical-based butterfly wings land exactly on
# the $5 grid (95/105), matching this task's own worked example.
FIVE_PCT_MOVES = [Decimal("0.05")] * 6


def _wide_context() -> ExpectedMoveContext:
    return _context(SPOT, WIDE_CHAIN, FIVE_PCT_MOVES)


class TestLongOptions:
    def test_long_call_targets_upside_expected_move_boundary(self):
        r = select_v4_strikes(StrategyCategory.LONG_CALL, _wide_context(), WIDE_CHAIN)
        assert r.status == "constructed"
        assert r.legs[0].action == "buy"
        assert r.legs[0].right == "call"
        assert r.legs[0].selected_strike == Decimal("110")  # spot + implied move (10)
        assert r.legs[0].expected_move_units == Decimal("1")

    def test_long_put_mirrors_downside(self):
        r = select_v4_strikes(StrategyCategory.LONG_PUT, _wide_context(), WIDE_CHAIN)
        assert r.status == "constructed"
        assert r.legs[0].right == "put"
        assert r.legs[0].selected_strike == Decimal("90")
        assert r.legs[0].expected_move_units == Decimal("-1")

    def test_long_call_not_simply_atm_plus_one(self):
        """The mandatory Section 10 regression: must NOT reduce to
        ATM+1 -- verified by the target landing a full expected-move
        away, not one adjacent strike."""
        r = select_v4_strikes(StrategyCategory.LONG_CALL, _wide_context(), WIDE_CHAIN)
        assert r.legs[0].selected_strike != Decimal("105")  # ATM+1 on this grid


class TestStraddle:
    def test_stays_atm_on_both_legs(self):
        r = select_v4_strikes(StrategyCategory.LONG_STRADDLE, _wide_context(), WIDE_CHAIN)
        assert r.status == "constructed"
        call_leg = next(leg for leg in r.legs if leg.right == "call")
        put_leg = next(leg for leg in r.legs if leg.right == "put")
        assert call_leg.selected_strike == put_leg.selected_strike == Decimal("100")
        assert r.symmetry_error_pct == Decimal("0")

    def test_does_not_require_expected_move_evidence(self):
        """A straddle only needs ATM -- it must still construct with no
        implied move and no historical data at all."""
        bare = _context(SPOT, WIDE_CHAIN, None)
        r = select_v4_strikes(StrategyCategory.LONG_STRADDLE, bare, WIDE_CHAIN)
        assert r.status == "constructed"

    def test_shared_center_mismatch_is_unconstructable(self):
        mismatched = [
            _quote(Decimal("100"), "call", Decimal("5"), Decimal("5.10")),
            _quote(Decimal("99"), "put", Decimal("5"), Decimal("5.10")),
        ]
        ctx = _context(SPOT, mismatched, None)
        r = select_v4_strikes(StrategyCategory.LONG_STRADDLE, ctx, mismatched)
        assert r.status == "unconstructable"
        assert UNCONSTRUCTABLE_SHARED_CENTER_MISMATCH in r.reason_codes


class TestStrangle:
    def test_uses_full_implied_move_boundary_not_arbitrary_fraction(self):
        r = select_v4_strikes(StrategyCategory.LONG_STRANGLE, _wide_context(), WIDE_CHAIN)
        assert r.status == "constructed"
        assert r.lower_boundary == Decimal("90")
        assert r.upper_boundary == Decimal("110")
        assert r.width == Decimal("20")

    def test_put_below_spot_below_call(self):
        r = select_v4_strikes(StrategyCategory.LONG_STRANGLE, _wide_context(), WIDE_CHAIN)
        put_leg = next(leg for leg in r.legs if leg.right == "put")
        call_leg = next(leg for leg in r.legs if leg.right == "call")
        assert put_leg.selected_strike < SPOT < call_leg.selected_strike

    def test_unconstructable_without_any_move_evidence(self):
        bare = _context(SPOT, None, None)
        r = select_v4_strikes(StrategyCategory.LONG_STRANGLE, bare, WIDE_CHAIN)
        assert r.status == "unconstructable"
        assert UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED in r.reason_codes


class TestDebitSpreads:
    def test_bull_call_long_leg_atm_short_leg_at_upside_target(self):
        r = select_v4_strikes(StrategyCategory.BULL_CALL_SPREAD, _wide_context(), WIDE_CHAIN)
        assert r.status == "constructed"
        long_leg = next(leg for leg in r.legs if leg.action == "buy")
        short_leg = next(leg for leg in r.legs if leg.action == "sell")
        assert long_leg.selected_strike == Decimal("100")
        assert short_leg.selected_strike == Decimal("110")
        assert long_leg.selected_strike < short_leg.selected_strike

    def test_bear_put_mirrors_downside(self):
        r = select_v4_strikes(StrategyCategory.BEAR_PUT_SPREAD, _wide_context(), WIDE_CHAIN)
        long_leg = next(leg for leg in r.legs if leg.action == "buy")
        short_leg = next(leg for leg in r.legs if leg.action == "sell")
        assert long_leg.selected_strike == Decimal("100")
        assert short_leg.selected_strike == Decimal("90")
        assert short_leg.selected_strike < long_leg.selected_strike


class TestCreditSpreads:
    def test_put_credit_short_strike_is_downside_threshold(self):
        r = select_v4_strikes(StrategyCategory.PUT_CREDIT_SPREAD, _wide_context(), WIDE_CHAIN)
        assert r.status == "constructed"
        short_leg = next(leg for leg in r.legs if leg.action == "sell")
        long_leg = next(leg for leg in r.legs if leg.action == "buy")
        assert short_leg.selected_strike == Decimal("90")
        assert long_leg.selected_strike == Decimal("85")  # next real strike beyond
        assert CHAIN_GRANULARITY_MINIMUM_WING in r.reason_codes

    def test_call_credit_short_strike_is_upside_threshold(self):
        r = select_v4_strikes(StrategyCategory.CALL_CREDIT_SPREAD, _wide_context(), WIDE_CHAIN)
        short_leg = next(leg for leg in r.legs if leg.action == "sell")
        long_leg = next(leg for leg in r.legs if leg.action == "buy")
        assert short_leg.selected_strike == Decimal("110")
        assert long_leg.selected_strike == Decimal("115")

    def test_no_protective_wing_available_is_unconstructable(self):
        # Implied move boundary lands exactly at the chain's own edge --
        # no further-OTM strike exists to build a protective wing.
        narrow = _chain(SPOT, [Decimal(s) for s in (90, 95, 100, 105, 110)])
        ctx = _context(SPOT, narrow, None)
        assert ctx.upper_implied_boundary == Decimal("110")
        r = select_v4_strikes(StrategyCategory.CALL_CREDIT_SPREAD, ctx, narrow)
        assert r.status == "unconstructable"
        assert UNCONSTRUCTABLE_NO_PROTECTIVE_WING_AVAILABLE in r.reason_codes


class TestIronCondor:
    def test_shorts_sell_the_expected_move_not_adjacent_strikes(self):
        """The mandatory Section 13 fix: short strikes must be the real
        range boundary (90/110), never merely ATM +/- 1 (95/105)."""
        r = select_v4_strikes(StrategyCategory.IRON_CONDOR, _wide_context(), WIDE_CHAIN)
        assert r.status == "constructed"
        assert r.lower_boundary == Decimal("90")
        assert r.upper_boundary == Decimal("110")
        assert r.lower_boundary != Decimal("95")
        assert r.upper_boundary != Decimal("105")

    def test_wings_further_out_than_shorts(self):
        r = select_v4_strikes(StrategyCategory.IRON_CONDOR, _wide_context(), WIDE_CHAIN)
        strikes_by_action = sorted(leg.selected_strike for leg in r.legs)
        assert strikes_by_action == [Decimal(s) for s in (85, 90, 110, 115)]


class TestButterflies:
    def test_center_at_spot_width_from_historical_median_not_one_strike_step(self):
        """The mandatory Section 14 fix: wing width must reflect real
        expected-move evidence, never a bare one-listed-strike-step."""
        r = select_v4_strikes(StrategyCategory.LONG_CALL_BUTTERFLY, _wide_context(), WIDE_CHAIN)
        assert r.status == "constructed"
        center_leg = next(leg for leg in r.legs if leg.quantity == 2)
        assert center_leg.selected_strike == Decimal("100")
        assert r.lower_boundary == Decimal("95")
        assert r.upper_boundary == Decimal("105")
        assert r.width == Decimal("10")

    def test_center_leg_is_sold_double(self):
        r = select_v4_strikes(StrategyCategory.LONG_CALL_BUTTERFLY, _wide_context(), WIDE_CHAIN)
        center_leg = next(leg for leg in r.legs if leg.selected_strike == Decimal("100"))
        assert center_leg.action == "sell"
        assert center_leg.quantity == 2

    def test_not_accidentally_narrow_when_market_implies_wider_move(self):
        """Section 14's own warning: prefers the tighter historical
        regime over the wider current implied move, deliberately -- so
        the wing width here (10) is narrower than the iron condor's
        implied-move-derived range width (20) on the identical chain."""
        butterfly = select_v4_strikes(
            StrategyCategory.LONG_CALL_BUTTERFLY, _wide_context(), WIDE_CHAIN
        )
        condor = select_v4_strikes(StrategyCategory.IRON_CONDOR, _wide_context(), WIDE_CHAIN)
        assert butterfly.width < condor.width

    def test_falls_back_to_implied_move_when_no_historical_evidence(self):
        ctx = _context(SPOT, WIDE_CHAIN, None)  # no historical data at all
        r = select_v4_strikes(StrategyCategory.LONG_CALL_BUTTERFLY, ctx, WIDE_CHAIN)
        assert r.status == "constructed"
        assert r.lower_boundary == Decimal("90")  # full implied move, not 95
        assert r.upper_boundary == Decimal("110")

    def test_iron_butterfly_shares_one_center_strike(self):
        r = select_v4_strikes(StrategyCategory.IRON_BUTTERFLY, _wide_context(), WIDE_CHAIN)
        assert r.status == "constructed"
        short_put = next(leg for leg in r.legs if leg.action == "sell" and leg.right == "put")
        short_call = next(leg for leg in r.legs if leg.action == "sell" and leg.right == "call")
        assert short_put.selected_strike == short_call.selected_strike == Decimal("100")


class TestUnconstructableFailsHonestly:
    def test_no_strikes_at_all(self):
        ctx = _context(SPOT, WIDE_CHAIN, FIVE_PCT_MOVES)
        r = select_v4_strikes(StrategyCategory.LONG_CALL, ctx, [])
        assert r.status == "unconstructable"
        assert UNCONSTRUCTABLE_MISSING_LEG_QUOTE in r.reason_codes

    def test_implied_move_required_and_absent(self):
        bare = _context(SPOT, None, None)
        for category in (
            StrategyCategory.LONG_CALL,
            StrategyCategory.LONG_STRANGLE,
            StrategyCategory.BULL_CALL_SPREAD,
            StrategyCategory.PUT_CREDIT_SPREAD,
            StrategyCategory.IRON_CONDOR,
            StrategyCategory.LONG_CALL_BUTTERFLY,
            StrategyCategory.IRON_BUTTERFLY,
        ):
            r = select_v4_strikes(category, bare, WIDE_CHAIN)
            assert r.status == "unconstructable", category
            assert UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED in r.reason_codes, category

    def test_never_fabricates_a_strike_on_failure(self):
        bare = _context(SPOT, None, None)
        r = select_v4_strikes(StrategyCategory.LONG_STRANGLE, bare, WIDE_CHAIN)
        assert all(leg.selected_strike is None for leg in r.legs) or r.legs == ()


class TestOrderingInvariants:
    """Section 28's mandatory list, checked directly against real
    constructed results on the wide chain."""

    def test_bull_call_long_lt_short(self):
        r = select_v4_strikes(StrategyCategory.BULL_CALL_SPREAD, _wide_context(), WIDE_CHAIN)
        long_leg = next(leg for leg in r.legs if leg.action == "buy")
        short_leg = next(leg for leg in r.legs if leg.action == "sell")
        assert long_leg.selected_strike < short_leg.selected_strike

    def test_bear_put_long_gt_short(self):
        r = select_v4_strikes(StrategyCategory.BEAR_PUT_SPREAD, _wide_context(), WIDE_CHAIN)
        long_leg = next(leg for leg in r.legs if leg.action == "buy")
        short_leg = next(leg for leg in r.legs if leg.action == "sell")
        assert long_leg.selected_strike > short_leg.selected_strike

    def test_put_credit_long_lt_short(self):
        r = select_v4_strikes(StrategyCategory.PUT_CREDIT_SPREAD, _wide_context(), WIDE_CHAIN)
        long_leg = next(leg for leg in r.legs if leg.action == "buy")
        short_leg = next(leg for leg in r.legs if leg.action == "sell")
        assert long_leg.selected_strike < short_leg.selected_strike

    def test_call_credit_short_lt_long(self):
        r = select_v4_strikes(StrategyCategory.CALL_CREDIT_SPREAD, _wide_context(), WIDE_CHAIN)
        long_leg = next(leg for leg in r.legs if leg.action == "buy")
        short_leg = next(leg for leg in r.legs if leg.action == "sell")
        assert short_leg.selected_strike < long_leg.selected_strike

    def test_butterfly_lower_lt_center_lt_upper(self):
        r = select_v4_strikes(StrategyCategory.LONG_CALL_BUTTERFLY, _wide_context(), WIDE_CHAIN)
        strikes = sorted({leg.selected_strike for leg in r.legs})
        assert strikes == [Decimal("95"), Decimal("100"), Decimal("105")]

    def test_iron_condor_long_put_lt_short_put_lt_short_call_lt_long_call(self):
        r = select_v4_strikes(StrategyCategory.IRON_CONDOR, _wide_context(), WIDE_CHAIN)
        long_put = next(leg for leg in r.legs if leg.right == "put" and leg.action == "buy")
        short_put = next(leg for leg in r.legs if leg.right == "put" and leg.action == "sell")
        short_call = next(leg for leg in r.legs if leg.right == "call" and leg.action == "sell")
        long_call = next(leg for leg in r.legs if leg.right == "call" and leg.action == "buy")
        assert (
            long_put.selected_strike
            < short_put.selected_strike
            < short_call.selected_strike
            < long_call.selected_strike
        )

    def test_straddle_same_strike(self):
        r = select_v4_strikes(StrategyCategory.LONG_STRADDLE, _wide_context(), WIDE_CHAIN)
        assert r.legs[0].selected_strike == r.legs[1].selected_strike

    def test_strangle_put_lt_spot_lt_call(self):
        r = select_v4_strikes(StrategyCategory.LONG_STRANGLE, _wide_context(), WIDE_CHAIN)
        put_leg = next(leg for leg in r.legs if leg.right == "put")
        call_leg = next(leg for leg in r.legs if leg.right == "call")
        assert put_leg.selected_strike < SPOT < call_leg.selected_strike


class TestSymmetry:
    def test_butterfly_symmetric_on_a_symmetric_grid(self):
        r = select_v4_strikes(StrategyCategory.LONG_CALL_BUTTERFLY, _wide_context(), WIDE_CHAIN)
        assert r.symmetry_error_pct == Decimal("0")

    def test_iron_butterfly_symmetric_on_a_symmetric_grid(self):
        r = select_v4_strikes(StrategyCategory.IRON_BUTTERFLY, _wide_context(), WIDE_CHAIN)
        assert r.symmetry_error_pct == Decimal("0")

    def test_strangle_symmetric_on_a_symmetric_grid(self):
        r = select_v4_strikes(StrategyCategory.LONG_STRANGLE, _wide_context(), WIDE_CHAIN)
        assert r.symmetry_error_pct == Decimal("0")

    def test_never_requires_impossible_perfect_symmetry(self):
        """A coarse, asymmetric-around-target grid must still construct
        -- symmetry error is measured and reported, never enforced."""
        lumpy = _chain(SPOT, [Decimal(s) for s in (70, 93, 100, 107, 130)])
        ctx = _context(SPOT, lumpy, FIVE_PCT_MOVES)
        r = select_v4_strikes(StrategyCategory.LONG_CALL_BUTTERFLY, ctx, lumpy)
        assert r.status == "constructed"
        assert r.symmetry_error_pct is not None


class TestScaleInvariance:
    """Section 26: proportionally-scaled setups should produce
    proportionally-equivalent normalized geometry -- proof V4.3 is not
    dollar-price dependent."""

    def test_iron_condor_expected_move_units_match_across_price_scales(self):
        spot_a, spot_b = Decimal("100"), Decimal("500")
        ratio = spot_b / spot_a
        strikes_a = _grid(spot_a, Decimal("5"), 4)
        strikes_b = [s * ratio for s in strikes_a]
        chain_a, chain_b = _chain(spot_a, strikes_a), _chain(spot_b, strikes_b)
        ctx_a, ctx_b = (
            _context(spot_a, chain_a, FIVE_PCT_MOVES),
            _context(spot_b, chain_b, FIVE_PCT_MOVES),
        )

        r_a = select_v4_strikes(StrategyCategory.IRON_CONDOR, ctx_a, chain_a)
        r_b = select_v4_strikes(StrategyCategory.IRON_CONDOR, ctx_b, chain_b)

        assert r_a.status == r_b.status == "constructed"
        assert r_a.width_in_expected_move_units == r_b.width_in_expected_move_units
        assert (r_a.lower_boundary / spot_a) == (r_b.lower_boundary / spot_b)
        assert (r_a.upper_boundary / spot_a) == (r_b.upper_boundary / spot_b)

    def test_butterfly_moneyness_matches_across_price_scales(self):
        spot_a, spot_b = Decimal("100"), Decimal("500")
        ratio = spot_b / spot_a
        strikes_a = _grid(spot_a, Decimal("5"), 4)
        strikes_b = [s * ratio for s in strikes_a]
        chain_a, chain_b = _chain(spot_a, strikes_a), _chain(spot_b, strikes_b)
        ctx_a, ctx_b = (
            _context(spot_a, chain_a, FIVE_PCT_MOVES),
            _context(spot_b, chain_b, FIVE_PCT_MOVES),
        )

        r_a = select_v4_strikes(StrategyCategory.LONG_CALL_BUTTERFLY, ctx_a, chain_a)
        r_b = select_v4_strikes(StrategyCategory.LONG_CALL_BUTTERFLY, ctx_b, chain_b)

        legs_a = sorted(r_a.legs, key=lambda leg: leg.selected_strike)
        legs_b = sorted(r_b.legs, key=lambda leg: leg.selected_strike)
        for leg_a, leg_b in zip(legs_a, legs_b, strict=True):
            assert leg_a.moneyness_pct == leg_b.moneyness_pct


class TestChainGranularity:
    """Section 27: the resolver must gracefully map targets to real
    listed strikes regardless of a ticker's real strike spacing."""

    def test_graceful_across_spacings(self):
        """ "Gracefully map" does not mean "always succeeds" -- a $5-wide
        pinning target genuinely cannot be honored on a $10-step grid
        (Section 21's own "wing overlaps center" failure mode is real
        here, not a bug). What must hold at every real spacing: no
        crash, and every constructed result's ordering invariant
        genuinely holds -- never a silently broken/overlapping
        structure. The wider iron condor (its own target is the full
        $10-$20 implied-move range) succeeds at every spacing tested."""
        for step in (Decimal("0.5"), Decimal("1"), Decimal("2.5"), Decimal("5"), Decimal("10")):
            # Enough strikes each side to reach the $10 implied-move
            # boundary AND leave room for a protective wing beyond it,
            # regardless of how coarse this ticker's real spacing is.
            count_each_side = int(Decimal("15") / step) + 1
            strikes = _grid(SPOT, step, count_each_side)
            chain = _chain(SPOT, strikes)
            ctx = _context(SPOT, chain, FIVE_PCT_MOVES)

            condor = select_v4_strikes(StrategyCategory.IRON_CONDOR, ctx, chain)
            assert condor.status == "constructed", step
            assert condor.lower_boundary < condor.upper_boundary, step

            butterfly = select_v4_strikes(StrategyCategory.LONG_CALL_BUTTERFLY, ctx, chain)
            assert butterfly.status in ("constructed", "unconstructable"), step
            if butterfly.status == "constructed":
                assert (
                    butterfly.lower_boundary < butterfly.center_target < butterfly.upper_boundary
                ), step
            else:
                assert butterfly.reason_codes, step  # honest failure, never a silent one


class TestMarketCoherenceSurfacing:
    def _coherence(self, status: str) -> MarketCoherenceResult:
        return MarketCoherenceResult(
            status=status,
            decision_underlying_price=SPOT,
            decision_underlying_observed_at=NOW,
            entry_underlying_price=SPOT,
            entry_underlying_observed_at=NOW,
            drift_pct=Decimal("0"),
            live_refresh_attempted=False,
            live_refresh_succeeded=None,
            reason="test",
        )

    def test_stale_status_is_surfaced_not_blocking(self):
        r = select_v4_strikes(
            StrategyCategory.LONG_STRADDLE, _wide_context(), WIDE_CHAIN, self._coherence("stale")
        )
        assert r.status == "constructed"  # never rejected
        assert MARKET_COHERENCE_NOT_FRESH in r.reason_codes

    def test_fresh_status_adds_no_reason_code(self):
        r = select_v4_strikes(
            StrategyCategory.LONG_STRADDLE, _wide_context(), WIDE_CHAIN, self._coherence("fresh")
        )
        assert MARKET_COHERENCE_NOT_FRESH not in r.reason_codes

    def test_none_adds_no_reason_code(self):
        r = select_v4_strikes(StrategyCategory.LONG_STRADDLE, _wide_context(), WIDE_CHAIN, None)
        assert MARKET_COHERENCE_NOT_FRESH not in r.reason_codes

    def test_surfaced_even_on_unconstructable_result(self):
        bare = _context(SPOT, None, None)
        r = select_v4_strikes(
            StrategyCategory.LONG_STRANGLE, bare, WIDE_CHAIN, self._coherence("unknown_age")
        )
        assert r.status == "unconstructable"
        assert MARKET_COHERENCE_NOT_FRESH in r.reason_codes


class TestEngineVersion:
    def test_every_result_carries_the_engine_version(self):
        r = select_v4_strikes(StrategyCategory.LONG_STRADDLE, _wide_context(), WIDE_CHAIN)
        assert r.engine_version == "expected_move_v1"
