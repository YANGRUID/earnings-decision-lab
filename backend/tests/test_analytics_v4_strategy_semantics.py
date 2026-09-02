"""V4.1 methodology foundation (2026-08-31) -- locks the real economic
meaning of every strategy family, so a future change can never silently
regress the exact correction this registry exists for: a long call
butterfly is a net-debit structure that is NOT a generic long-vol bet.
"""

from analytics.decision.v4_strategy_semantics import (
    all_strategy_semantics,
    get_strategy_semantics,
)
from analytics.options.strategy_candidates import StrategyCategory


def test_every_real_strategy_category_is_classified():
    registry = all_strategy_semantics()
    for category in StrategyCategory:
        assert category in registry, f"{category} has no StrategySemantics entry"


class TestLongVolFamily:
    def test_long_straddle_is_large_move_long_vol(self):
        s = get_strategy_semantics(StrategyCategory.LONG_STRADDLE)
        assert s.directional_intent == "direction_agnostic"
        assert s.move_intent == "large_move"
        assert s.volatility_intent == "long_realized_move"
        assert s.payoff_shape == "two_sided_convex"

    def test_long_strangle_is_large_move_long_vol(self):
        s = get_strategy_semantics(StrategyCategory.LONG_STRANGLE)
        assert s.directional_intent == "direction_agnostic"
        assert s.move_intent == "large_move"
        assert s.volatility_intent == "long_realized_move"
        assert s.payoff_shape == "two_sided_convex"

    def test_long_call_is_bullish_convex_long_vol(self):
        s = get_strategy_semantics(StrategyCategory.LONG_CALL)
        assert s.directional_intent == "bullish"
        assert s.volatility_intent == "long_realized_move"
        assert s.payoff_shape == "single_sided_convex"

    def test_long_put_is_bearish_convex_long_vol(self):
        s = get_strategy_semantics(StrategyCategory.LONG_PUT)
        assert s.directional_intent == "bearish"
        assert s.volatility_intent == "long_realized_move"
        assert s.payoff_shape == "single_sided_convex"


class TestButterflyIsNotGenericLongVol:
    """The core regression this whole registry exists to prevent."""

    def test_long_call_butterfly_is_pinning_not_long_vol(self):
        s = get_strategy_semantics(StrategyCategory.LONG_CALL_BUTTERFLY)
        assert s.move_intent == "small_move_pinning"
        assert s.volatility_intent == "short_realized_move"
        assert s.volatility_intent != "long_realized_move"
        assert s.payoff_shape == "tent_pinning"
        assert s.directional_intent == "neutral_range"

    def test_iron_butterfly_is_also_pinning(self):
        s = get_strategy_semantics(StrategyCategory.IRON_BUTTERFLY)
        assert s.move_intent == "small_move_pinning"
        assert s.volatility_intent == "short_realized_move"
        assert s.payoff_shape == "tent_pinning"


class TestShortVolFamily:
    def test_iron_condor_is_range_bound_short_vol(self):
        s = get_strategy_semantics(StrategyCategory.IRON_CONDOR)
        assert s.directional_intent == "neutral_range"
        assert s.move_intent == "range_bound"
        assert s.volatility_intent == "short_realized_move"
        assert s.payoff_shape == "range_credit"

    def test_put_credit_spread_is_bullish_short_vol(self):
        s = get_strategy_semantics(StrategyCategory.PUT_CREDIT_SPREAD)
        assert s.directional_intent == "bullish"
        assert s.volatility_intent == "short_realized_move"

    def test_call_credit_spread_is_bearish_short_vol(self):
        s = get_strategy_semantics(StrategyCategory.CALL_CREDIT_SPREAD)
        assert s.directional_intent == "bearish"
        assert s.volatility_intent == "short_realized_move"


class TestV4Point2CreditSpreadCorrection:
    """V4.2 re-audit (this task's own Section 1/2): credit spreads are
    one-sided directional thresholds, NOT center-seeking pinning bets
    like a butterfly -- they must never share a move_intent value."""

    def test_put_credit_spread_is_a_directional_threshold_not_pinning(self):
        s = get_strategy_semantics(StrategyCategory.PUT_CREDIT_SPREAD)
        assert s.move_intent == "directional_threshold"
        assert s.move_intent != "small_move_pinning"

    def test_call_credit_spread_is_a_directional_threshold_not_pinning(self):
        s = get_strategy_semantics(StrategyCategory.CALL_CREDIT_SPREAD)
        assert s.move_intent == "directional_threshold"
        assert s.move_intent != "small_move_pinning"

    def test_credit_spreads_and_butterflies_now_have_distinct_move_intents(self):
        put_credit = get_strategy_semantics(StrategyCategory.PUT_CREDIT_SPREAD)
        butterfly = get_strategy_semantics(StrategyCategory.LONG_CALL_BUTTERFLY)
        assert put_credit.move_intent != butterfly.move_intent


def test_every_registry_entry_has_real_failure_modes():
    for category, semantics in all_strategy_semantics().items():
        assert len(semantics.failure_modes) > 0, f"{category} has no failure_modes"


class TestDirectionalDebitSpreads:
    def test_bull_call_spread_is_bullish_and_not_cleanly_long_or_short_vol(self):
        s = get_strategy_semantics(StrategyCategory.BULL_CALL_SPREAD)
        assert s.directional_intent == "bullish"
        assert s.volatility_intent == "mixed_path_dependent"
        assert s.payoff_shape == "vertical_bounded_directional"

    def test_bear_put_spread_is_bearish_and_not_cleanly_long_or_short_vol(self):
        s = get_strategy_semantics(StrategyCategory.BEAR_PUT_SPREAD)
        assert s.directional_intent == "bearish"
        assert s.volatility_intent == "mixed_path_dependent"
        assert s.payoff_shape == "vertical_bounded_directional"


def test_registry_lookup_never_conflates_debit_sign_with_volatility_intent():
    """A cross-check spanning the whole registry: at least one net-debit
    family (the butterfly) must NOT be long_realized_move, proving this
    registry genuinely doesn't just re-derive volatility_intent from
    debit/credit sign the way V3's own _volatility_fit does."""
    registry = all_strategy_semantics()
    debit_families_that_are_not_long_vol = [
        category
        for category, semantics in registry.items()
        if category in (StrategyCategory.LONG_CALL_BUTTERFLY, StrategyCategory.BULL_CALL_SPREAD)
        and semantics.volatility_intent != "long_realized_move"
    ]
    assert StrategyCategory.LONG_CALL_BUTTERFLY in debit_families_that_are_not_long_vol
