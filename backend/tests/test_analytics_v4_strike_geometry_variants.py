"""V4.3.1 strike geometry candidate-set tests (2026-09-03). Covers
per-strategy variant methodology (Sections 8-13), candidate-explosion
budget (Section 15), contract deduplication (Section 16), temporal
coherence (Section 17), semantic filtering (Section 19), ordering/
symmetry/determinism, and scale invariance. Every case was hand-
verified against a manual synthetic-chain run before being written
here; none were fitted against the 7 real settled trades."""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_expected_move import ExpectedMoveContext, derive_expected_move_context
from analytics.decision.v4_market_view import derive_v4_market_view
from analytics.decision.v4_strike_geometry_variants import (
    MAX_VARIANTS_PER_STRATEGY,
    generate_all_strategy_variant_sets,
    generate_strike_geometry_variants,
)
from analytics.options.strategy_candidates import StrategyCategory
from models.enums import DecisionDirection, DecisionVolatilityView
from providers.types import OptionQuote

EXP = date(2026, 9, 18)
NOW = datetime(2026, 8, 17, tzinfo=UTC)
SPOT = Decimal("100")


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


WIDE_STRIKES = _grid(SPOT, Decimal("5"), 4)  # 80..120
WIDE_CHAIN = _chain(SPOT, WIDE_STRIKES)
FIVE_PCT_MOVES = [
    Decimal("0.03"),
    Decimal("0.05"),
    Decimal("0.04"),
    Decimal("0.06"),
    Decimal("0.07"),
]


def _context(historical_moves=FIVE_PCT_MOVES) -> ExpectedMoveContext:
    return derive_expected_move_context(
        spot=SPOT,
        observed_at=NOW,
        expiration=EXP,
        quotes_for_expiration=WIDE_CHAIN,
        historical_next_day_move_pcts=historical_moves,
    )


class TestLongOptionVariants:
    def test_three_variants_atm_modest_full(self):
        cs = generate_strike_geometry_variants(StrategyCategory.LONG_CALL, _context(), WIDE_CHAIN)
        assert len(cs.variants) == 3
        strikes = {v.variant_id: v.result.legs[0].selected_strike for v in cs.variants}
        assert strikes["long_call_ATM"] == Decimal("100")
        assert strikes["long_call_FULL_EXPECTED_MOVE"] == Decimal("110")

    def test_atm_variant_needs_no_expected_move_evidence(self):
        """ATM never requires implied move or historical data -- only
        the two OTM variants do."""
        bare = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=EXP,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=None,
        )
        cs = generate_strike_geometry_variants(StrategyCategory.LONG_CALL, bare, WIDE_CHAIN)
        atm = next(v for v in cs.variants if v.variant_id == "long_call_ATM")
        assert atm.result.status == "constructed"
        others = [v for v in cs.variants if v.variant_id != "long_call_ATM"]
        assert all(v.result.status == "unconstructable" for v in others)

    def test_long_put_mirrors(self):
        cs = generate_strike_geometry_variants(StrategyCategory.LONG_PUT, _context(), WIDE_CHAIN)
        strikes = {v.variant_id: v.result.legs[0].selected_strike for v in cs.variants}
        assert strikes["long_put_FULL_EXPECTED_MOVE"] == Decimal("90")

    def test_exactly_one_base_geometry(self):
        cs = generate_strike_geometry_variants(StrategyCategory.LONG_CALL, _context(), WIDE_CHAIN)
        base_count = sum(1 for v in cs.variants if v.is_base_geometry)
        assert base_count == 1
        assert cs.base_geometry.variant_id == "long_call_FULL_EXPECTED_MOVE"


class TestStrangleVariants:
    def test_two_variants_half_and_full(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_STRANGLE, _context(), WIDE_CHAIN
        )
        assert len(cs.variants) == 2
        widths = {v.variant_id: v.result.width for v in cs.variants}
        assert widths["long_strangle_HALF_MOVE"] == Decimal("10")
        assert widths["long_strangle_FULL_MOVE"] == Decimal("20")

    def test_symmetric_around_spot(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_STRANGLE, _context(), WIDE_CHAIN
        )
        for v in cs.variants:
            put_leg = next(leg for leg in v.result.legs if leg.right == "put")
            call_leg = next(leg for leg in v.result.legs if leg.right == "call")
            assert put_leg.selected_strike < SPOT < call_leg.selected_strike


class TestDebitSpreadVariants:
    def test_two_variants_tight_and_full(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.BULL_CALL_SPREAD, _context(), WIDE_CHAIN
        )
        assert len(cs.variants) == 2
        widths = {v.variant_id: v.result.width for v in cs.variants}
        assert widths["bull_call_spread_TIGHT_SHORT"] == Decimal("5")
        assert widths["bull_call_spread_FULL_MOVE_SHORT"] == Decimal("10")

    def test_ordering_preserved_in_every_variant(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.BULL_CALL_SPREAD, _context(), WIDE_CHAIN
        )
        for v in cs.variants:
            long_leg = next(leg for leg in v.result.legs if leg.action == "buy")
            short_leg = next(leg for leg in v.result.legs if leg.action == "sell")
            assert long_leg.selected_strike < short_leg.selected_strike


class TestCreditSpreadVariants:
    def test_two_variants_minimum_and_wider_wing(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.PUT_CREDIT_SPREAD, _context(), WIDE_CHAIN
        )
        assert len(cs.variants) == 2
        widths = {v.variant_id: v.result.width for v in cs.variants}
        assert widths["put_credit_spread_MINIMUM_WING"] == Decimal("5")
        assert widths["put_credit_spread_WIDER_WING"] == Decimal("10")

    def test_short_strike_identical_across_both_variants(self):
        """Only the wing width should vary -- the short strike (the
        real economic thesis, per V4.2) stays fixed."""
        cs = generate_strike_geometry_variants(
            StrategyCategory.PUT_CREDIT_SPREAD, _context(), WIDE_CHAIN
        )
        shorts = {
            next(leg.selected_strike for leg in v.result.legs if leg.action == "sell")
            for v in cs.variants
        }
        assert len(shorts) == 1

    def test_wider_wing_unconstructable_when_chain_too_narrow(self):
        narrow = _chain(SPOT, [Decimal(s) for s in (90, 95, 100, 105, 110)])
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=EXP,
            quotes_for_expiration=narrow,
            historical_next_day_move_pcts=None,
        )
        cs = generate_strike_geometry_variants(StrategyCategory.CALL_CREDIT_SPREAD, ctx, narrow)
        wide = next(v for v in cs.variants if v.variant_id == "call_credit_spread_WIDER_WING")
        assert wide.result.status == "unconstructable"


class TestIronCondorVariants:
    def test_two_variants_narrower_and_full(self):
        cs = generate_strike_geometry_variants(StrategyCategory.IRON_CONDOR, _context(), WIDE_CHAIN)
        assert len(cs.variants) == 2
        boundaries = {
            v.variant_id: (v.result.lower_boundary, v.result.upper_boundary) for v in cs.variants
        }
        assert boundaries["iron_condor_FULL_RANGE"] == (Decimal("90"), Decimal("110"))
        narrower = boundaries["iron_condor_NARROWER_RANGE"]
        assert narrower[0] > Decimal("90")
        assert narrower[1] < Decimal("110")

    def test_ordering_holds_in_every_variant(self):
        cs = generate_strike_geometry_variants(StrategyCategory.IRON_CONDOR, _context(), WIDE_CHAIN)
        for v in cs.variants:
            if v.result.status != "constructed":
                continue
            strikes = sorted(leg.selected_strike for leg in v.result.legs)
            assert strikes == sorted(set(strikes))  # all four distinct and sortable


class TestButterflyVariants:
    def test_narrow_pin_present_when_quartiles_available(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_CALL_BUTTERFLY, _context(), WIDE_CHAIN
        )
        ids = {v.variant_id for v in cs.variants}
        assert "long_call_butterfly_NARROW_PIN" in ids
        assert "long_call_butterfly_BASE_HISTORICAL_RANGE" in ids
        assert "long_call_butterfly_WIDER_RANGE" in ids
        assert len(cs.variants) == 3

    def test_narrow_pin_absent_below_quartile_minimum(self):
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=EXP,
            quotes_for_expiration=WIDE_CHAIN,
            historical_next_day_move_pcts=[
                Decimal("0.05"),
                Decimal("0.05"),
            ],  # N=2, below MIN_N_FOR_QUARTILES
        )
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_CALL_BUTTERFLY, ctx, WIDE_CHAIN
        )
        ids = {v.variant_id for v in cs.variants}
        assert "long_call_butterfly_NARROW_PIN" not in ids
        assert len(cs.variants) == 2

    def test_wider_range_is_wider_than_base(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_CALL_BUTTERFLY, _context(), WIDE_CHAIN
        )
        widths = {v.variant_id: v.result.width for v in cs.variants}
        assert (
            widths["long_call_butterfly_WIDER_RANGE"]
            >= widths["long_call_butterfly_BASE_HISTORICAL_RANGE"]
        )

    def test_never_averages_anchors_into_one_number(self):
        """Each anchor must produce its own distinct geometry -- never
        one blended magic-number width."""
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_CALL_BUTTERFLY, _context(), WIDE_CHAIN
        )
        sources = {v.variant_id: v.result.reason_codes for v in cs.variants}
        assert (
            sources["long_call_butterfly_BASE_HISTORICAL_RANGE"]
            != sources["long_call_butterfly_WIDER_RANGE"]
        )

    def test_center_leg_doubled_in_every_variant(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_CALL_BUTTERFLY, _context(), WIDE_CHAIN
        )
        for v in cs.variants:
            if v.result.status != "constructed":
                continue
            center_legs = [leg for leg in v.result.legs if leg.quantity == 2]
            assert len(center_legs) == 1

    def test_iron_butterfly_shares_center_in_every_variant(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.IRON_BUTTERFLY, _context(), WIDE_CHAIN
        )
        for v in cs.variants:
            if v.result.status != "constructed":
                continue
            short_put = next(
                leg for leg in v.result.legs if leg.action == "sell" and leg.right == "put"
            )
            short_call = next(
                leg for leg in v.result.legs if leg.action == "sell" and leg.right == "call"
            )
            assert short_put.selected_strike == short_call.selected_strike


class TestStraddleHasNoMeaningfulVariant:
    def test_exactly_one_variant(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_STRADDLE, _context(), WIDE_CHAIN
        )
        assert len(cs.variants) == 1
        assert cs.variants[0].is_base_geometry is True


class TestCandidateBudget:
    def test_no_strategy_exceeds_max_variants(self):
        all_sets = generate_all_strategy_variant_sets(_context(), WIDE_CHAIN)
        for strategy, candidate_set in all_sets.items():
            assert len(candidate_set.variants) <= MAX_VARIANTS_PER_STRATEGY, strategy

    def test_total_variants_across_all_strategies_is_tens_not_hundreds(self):
        all_sets = generate_all_strategy_variant_sets(_context(), WIDE_CHAIN)
        total = sum(len(cs.variants) for cs in all_sets.values())
        assert total < 50, total


class TestContractDeduplication:
    def test_dedupes_shared_strikes_within_one_strategy(self):
        cs = generate_strike_geometry_variants(
            StrategyCategory.PUT_CREDIT_SPREAD, _context(), WIDE_CHAIN
        )
        # Both variants share the same short leg strike -- deduped
        # contract count must be strictly fewer than the naive sum of
        # every variant's own leg count.
        naive_leg_count = sum(len(v.result.legs) for v in cs.variants)
        assert len(cs.required_unique_contracts) < naive_leg_count

    def test_contracts_are_real_resolved_strikes_only(self):
        cs = generate_strike_geometry_variants(StrategyCategory.LONG_CALL, _context(), WIDE_CHAIN)
        for right, strike in cs.required_unique_contracts:
            assert right in ("call", "put")
            assert strike in WIDE_STRIKES

    def test_deduplication_across_all_strategies_shows_real_savings(self):
        all_sets = generate_all_strategy_variant_sets(_context(), WIDE_CHAIN)
        naive_total = sum(sum(len(v.result.legs) for v in cs.variants) for cs in all_sets.values())
        combined_unique: set[tuple[str, Decimal]] = set()
        for cs in all_sets.values():
            combined_unique.update(cs.required_unique_contracts)
        assert len(combined_unique) < naive_total


class TestTemporalCoherence:
    def test_every_variant_shares_the_same_observed_at(self):
        """Section 17: one shared point-in-time snapshot per candidate-
        set-generation call -- structurally guaranteed since every
        variant's leg quotes come from the same shared ``quotes`` list."""
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_CALL_BUTTERFLY, _context(), WIDE_CHAIN
        )
        timestamps = {v.result.expected_move_context.observed_at for v in cs.variants}
        assert timestamps == {NOW}


class TestSemanticFiltering:
    def test_all_candidates_ignores_market_view(self):
        mv = derive_v4_market_view(DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL)
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_CALL_BUTTERFLY,
            _context(),
            WIDE_CHAIN,
            filter_mode="ALL_CANDIDATES",
            market_view=mv,
        )
        assert len(cs.variants) == 3

    def test_semantically_plausible_only_skips_a_contradiction(self):
        mv = derive_v4_market_view(DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL)
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_CALL_BUTTERFLY,
            _context(),
            WIDE_CHAIN,
            filter_mode="SEMANTICALLY_PLAUSIBLE_ONLY",
            market_view=mv,
        )
        assert len(cs.variants) == 1
        assert cs.variants[0].result.status == "unconstructable"
        assert "SKIPPED_SEMANTIC_CONTRADICTION" in cs.variants[0].result.reason_codes

    def test_semantically_plausible_only_keeps_a_compatible_strategy(self):
        mv = derive_v4_market_view(DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL)
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_STRADDLE,
            _context(),
            WIDE_CHAIN,
            filter_mode="SEMANTICALLY_PLAUSIBLE_ONLY",
            market_view=mv,
        )
        assert len(cs.variants) == 1
        assert cs.variants[0].result.status == "constructed"

    def test_filtering_is_never_the_default(self):
        mv = derive_v4_market_view(DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL)
        cs = generate_strike_geometry_variants(
            StrategyCategory.LONG_CALL_BUTTERFLY, _context(), WIDE_CHAIN, market_view=mv
        )
        assert len(cs.variants) == 3  # unfiltered by default


class TestScaleInvariance:
    def test_debit_spread_widths_scale_proportionally(self):
        spot_a, spot_b = Decimal("100"), Decimal("500")
        ratio = spot_b / spot_a
        strikes_a = _grid(spot_a, Decimal("5"), 4)
        strikes_b = [s * ratio for s in strikes_a]
        chain_a, chain_b = _chain(spot_a, strikes_a), _chain(spot_b, strikes_b)
        ctx_a = derive_expected_move_context(
            spot=spot_a,
            observed_at=NOW,
            expiration=EXP,
            quotes_for_expiration=chain_a,
            historical_next_day_move_pcts=FIVE_PCT_MOVES,
        )
        ctx_b = derive_expected_move_context(
            spot=spot_b,
            observed_at=NOW,
            expiration=EXP,
            quotes_for_expiration=chain_b,
            historical_next_day_move_pcts=FIVE_PCT_MOVES,
        )
        cs_a = generate_strike_geometry_variants(StrategyCategory.BULL_CALL_SPREAD, ctx_a, chain_a)
        cs_b = generate_strike_geometry_variants(StrategyCategory.BULL_CALL_SPREAD, ctx_b, chain_b)
        for va, vb in zip(cs_a.variants, cs_b.variants, strict=True):
            if va.result.width_pct_of_spot is not None:
                assert va.result.width_pct_of_spot == vb.result.width_pct_of_spot


class TestDeterministicGeneration:
    def test_repeated_generation_is_identical(self):
        results = [
            generate_strike_geometry_variants(StrategyCategory.IRON_CONDOR, _context(), WIDE_CHAIN)
            for _ in range(5)
        ]
        first = results[0]
        for other in results[1:]:
            assert [v.variant_id for v in other.variants] == [v.variant_id for v in first.variants]
            assert other.required_unique_contracts == first.required_unique_contracts


class TestBaseGeometryAlwaysPresent:
    """Regression test: a missing-evidence failure for what WOULD have
    been the base geometry must still be marked as the base geometry
    (every candidate set has exactly one) -- caught a real bug where
    ``_no_evidence_variant`` hardcoded ``is_base=False``, causing
    ``next(v for v in variants if v.is_base_geometry)`` to raise
    StopIteration whenever the FULL/BASE variant itself had no
    evidence to construct from."""

    def test_every_strategy_has_exactly_one_base_geometry_even_with_no_evidence(self):
        bare = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=EXP,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=None,
        )
        all_sets = generate_all_strategy_variant_sets(bare, WIDE_CHAIN)
        for strategy, cs in all_sets.items():
            base_count = sum(1 for v in cs.variants if v.is_base_geometry)
            assert base_count == 1, strategy
            assert cs.base_geometry is not None, strategy


class TestNoRealizedOutcomeDependency:
    def test_signature_carries_no_outcome_field(self):
        import inspect

        sig = inspect.signature(generate_strike_geometry_variants)
        forbidden = {"realized_move", "pnl", "settlement", "exit_price", "outcome"}
        assert forbidden.isdisjoint(sig.parameters.keys())
