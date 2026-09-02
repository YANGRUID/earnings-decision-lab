"""V4.2 view <-> strategy semantic compatibility engine tests
(2026-09-01) -- this task's own mandatory test matrix (Section 24) plus
property/invariant tests (Section 25). Every case below was verified by
hand against analytics/decision/v4_compatibility.py's matrices before
being written here; none were fitted against the 7 real settled trades'
realized outcomes (Section 27's own explicit anti-fitting rule)."""

import pytest

from analytics.decision.v4_compatibility import (
    CONTRADICTION,
    DIRECTION_CONTRADICTION,
    GOOD,
    MOVE_INTENT_CONTRADICTION,
    STRONG,
    evaluate_semantic_compatibility,
)
from analytics.decision.v4_market_view import derive_v4_market_view
from analytics.decision.v4_strategy_semantics import get_strategy_semantics
from analytics.options.strategy_candidates import StrategyCategory
from models.enums import DecisionDirection, DecisionVolatilityView


def _compat(direction, vol, category):
    market_view = derive_v4_market_view(direction, vol)
    semantics = get_strategy_semantics(category)
    return evaluate_semantic_compatibility(market_view, semantics)


class TestNeutralLongVol:
    """The exact regression this whole task exists for."""

    def test_long_straddle_is_strong(self):
        r = _compat(
            DecisionDirection.NEUTRAL,
            DecisionVolatilityView.LONG_VOL,
            StrategyCategory.LONG_STRADDLE,
        )
        assert r.overall_semantic_compatibility == STRONG
        assert r.tier == "strong"

    def test_long_strangle_is_strong(self):
        r = _compat(
            DecisionDirection.NEUTRAL,
            DecisionVolatilityView.LONG_VOL,
            StrategyCategory.LONG_STRANGLE,
        )
        assert r.overall_semantic_compatibility == STRONG

    def test_long_call_butterfly_is_a_real_contradiction(self):
        """The mandatory regression: a NEUTRAL+LONG_VOL butterfly must
        NOT score well merely because it is net debit."""
        r = _compat(
            DecisionDirection.NEUTRAL,
            DecisionVolatilityView.LONG_VOL,
            StrategyCategory.LONG_CALL_BUTTERFLY,
        )
        assert r.overall_semantic_compatibility <= 0.25
        assert MOVE_INTENT_CONTRADICTION in r.reason_codes
        # Direction alone must not rescue the overall score.
        assert r.direction_compatibility == STRONG
        assert r.overall_semantic_compatibility < r.direction_compatibility

    def test_iron_condor_is_poor(self):
        r = _compat(
            DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, StrategyCategory.IRON_CONDOR
        )
        assert r.overall_semantic_compatibility <= 0.25


class TestNeutralShortVol:
    def test_iron_condor_is_strong(self):
        r = _compat(
            DecisionDirection.NEUTRAL,
            DecisionVolatilityView.SHORT_VOL,
            StrategyCategory.IRON_CONDOR,
        )
        assert r.overall_semantic_compatibility == STRONG

    def test_iron_butterfly_is_strong(self):
        r = _compat(
            DecisionDirection.NEUTRAL,
            DecisionVolatilityView.SHORT_VOL,
            StrategyCategory.IRON_BUTTERFLY,
        )
        assert r.overall_semantic_compatibility == STRONG

    def test_long_straddle_is_poor(self):
        r = _compat(
            DecisionDirection.NEUTRAL,
            DecisionVolatilityView.SHORT_VOL,
            StrategyCategory.LONG_STRADDLE,
        )
        assert r.overall_semantic_compatibility <= 0.25


class TestBullishLongVol:
    def test_long_call_is_strong(self):
        r = _compat(
            DecisionDirection.BULLISH, DecisionVolatilityView.LONG_VOL, StrategyCategory.LONG_CALL
        )
        assert r.overall_semantic_compatibility == STRONG

    def test_bull_call_spread_is_good_or_conditional(self):
        r = _compat(
            DecisionDirection.BULLISH,
            DecisionVolatilityView.LONG_VOL,
            StrategyCategory.BULL_CALL_SPREAD,
        )
        assert 0.5 <= r.overall_semantic_compatibility <= GOOD

    def test_bear_put_spread_is_a_direction_contradiction(self):
        r = _compat(
            DecisionDirection.BULLISH,
            DecisionVolatilityView.LONG_VOL,
            StrategyCategory.BEAR_PUT_SPREAD,
        )
        assert r.overall_semantic_compatibility == CONTRADICTION
        assert DIRECTION_CONTRADICTION in r.reason_codes

    def test_long_put_is_poor_direction_despite_perfect_convexity(self):
        """Section 11's own worked example: a long put's move/volatility
        scores are perfect here, but direction must still veto it."""
        r = _compat(
            DecisionDirection.BULLISH, DecisionVolatilityView.LONG_VOL, StrategyCategory.LONG_PUT
        )
        assert r.move_magnitude_compatibility == STRONG
        assert r.volatility_compatibility == STRONG
        assert r.direction_compatibility == CONTRADICTION
        assert r.overall_semantic_compatibility == CONTRADICTION


class TestBearishLongVolMirrorsBullish:
    def test_long_put_is_strong(self):
        r = _compat(
            DecisionDirection.BEARISH, DecisionVolatilityView.LONG_VOL, StrategyCategory.LONG_PUT
        )
        assert r.overall_semantic_compatibility == STRONG

    def test_bear_put_spread_is_good_or_conditional(self):
        r = _compat(
            DecisionDirection.BEARISH,
            DecisionVolatilityView.LONG_VOL,
            StrategyCategory.BEAR_PUT_SPREAD,
        )
        assert 0.5 <= r.overall_semantic_compatibility <= GOOD

    def test_long_call_is_a_direction_contradiction(self):
        r = _compat(
            DecisionDirection.BEARISH, DecisionVolatilityView.LONG_VOL, StrategyCategory.LONG_CALL
        )
        assert r.overall_semantic_compatibility == CONTRADICTION
        assert DIRECTION_CONTRADICTION in r.reason_codes


class TestBullishShortVol:
    def test_put_credit_spread_is_strong(self):
        r = _compat(
            DecisionDirection.BULLISH,
            DecisionVolatilityView.SHORT_VOL,
            StrategyCategory.PUT_CREDIT_SPREAD,
        )
        assert r.overall_semantic_compatibility == STRONG

    def test_call_credit_spread_is_poor_direction(self):
        r = _compat(
            DecisionDirection.BULLISH,
            DecisionVolatilityView.SHORT_VOL,
            StrategyCategory.CALL_CREDIT_SPREAD,
        )
        assert r.overall_semantic_compatibility == CONTRADICTION
        assert DIRECTION_CONTRADICTION in r.reason_codes


class TestBearishShortVol:
    def test_call_credit_spread_is_strong(self):
        r = _compat(
            DecisionDirection.BEARISH,
            DecisionVolatilityView.SHORT_VOL,
            StrategyCategory.CALL_CREDIT_SPREAD,
        )
        assert r.overall_semantic_compatibility == STRONG

    def test_put_credit_spread_is_poor(self):
        r = _compat(
            DecisionDirection.BEARISH,
            DecisionVolatilityView.SHORT_VOL,
            StrategyCategory.PUT_CREDIT_SPREAD,
        )
        assert r.overall_semantic_compatibility == CONTRADICTION


# --------------------------------------------------------------------------
# Property / invariant tests (Section 25)
# --------------------------------------------------------------------------


def test_debit_credit_sign_alone_never_determines_volatility_compatibility():
    """The literal invariant this whole engine exists to enforce: a
    long_call_butterfly (net debit) and an iron_condor (net credit) must
    score IDENTICALLY on volatility compatibility for the same view,
    since both share volatility_intent="short_realized_move" -- proving
    the score comes from real payoff geometry, never from
    strategy_candidates.py's own net_premium sign."""
    butterfly = _compat(
        DecisionDirection.NEUTRAL,
        DecisionVolatilityView.SHORT_VOL,
        StrategyCategory.LONG_CALL_BUTTERFLY,
    )
    condor = _compat(
        DecisionDirection.NEUTRAL, DecisionVolatilityView.SHORT_VOL, StrategyCategory.IRON_CONDOR
    )
    assert butterfly.volatility_compatibility == condor.volatility_compatibility == STRONG


@pytest.mark.parametrize(
    ("bullish_category", "bearish_category"),
    [
        (StrategyCategory.LONG_CALL, StrategyCategory.LONG_PUT),
        (StrategyCategory.BULL_CALL_SPREAD, StrategyCategory.BEAR_PUT_SPREAD),
        (StrategyCategory.PUT_CREDIT_SPREAD, StrategyCategory.CALL_CREDIT_SPREAD),
    ],
)
def test_reversing_direction_mirrors_directional_strategy_pairs(bullish_category, bearish_category):
    bullish_view_bullish_strategy = _compat(
        DecisionDirection.BULLISH, DecisionVolatilityView.LONG_VOL, bullish_category
    )
    bearish_view_bearish_strategy = _compat(
        DecisionDirection.BEARISH, DecisionVolatilityView.LONG_VOL, bearish_category
    )
    assert (
        bullish_view_bullish_strategy.overall_semantic_compatibility
        == bearish_view_bearish_strategy.overall_semantic_compatibility
    )
    bullish_view_bearish_strategy = _compat(
        DecisionDirection.BULLISH, DecisionVolatilityView.LONG_VOL, bearish_category
    )
    assert bullish_view_bearish_strategy.direction_compatibility == CONTRADICTION


@pytest.mark.parametrize(
    "category",
    [
        StrategyCategory.LONG_STRADDLE,
        StrategyCategory.IRON_CONDOR,
        StrategyCategory.LONG_CALL_BUTTERFLY,
    ],
)
def test_long_vol_vs_short_vol_materially_changes_compatibility(category):
    long_vol = _compat(DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, category)
    short_vol = _compat(DecisionDirection.NEUTRAL, DecisionVolatilityView.SHORT_VOL, category)
    assert long_vol.overall_semantic_compatibility != short_vol.overall_semantic_compatibility


def test_unknown_volatility_view_degrades_to_conditional_never_false_certainty():
    """A missing volatility_view (e.g. a real pre-Phase-4 row) must
    never silently read as a strong OR a contradiction -- always the
    honest middle."""
    r = _compat(DecisionDirection.NEUTRAL, None, StrategyCategory.LONG_CALL_BUTTERFLY)
    assert r.volatility_compatibility == 0.5
    assert "MARKET_VIEW_UNDERSPECIFIED" in r.reason_codes


def test_neutral_vol_view_degrades_confidence_rather_than_asserting_certainty():
    r = _compat(
        DecisionDirection.NEUTRAL,
        DecisionVolatilityView.NEUTRAL_VOL,
        StrategyCategory.LONG_CALL_BUTTERFLY,
    )
    assert r.overall_semantic_compatibility < STRONG
    assert "MARKET_VIEW_UNDERSPECIFIED" in r.reason_codes


def test_overall_is_always_the_minimum_component_never_an_average():
    """Direct enforcement of Section 11's own worked concern."""
    for direction in DecisionDirection:
        for vol in (*DecisionVolatilityView, None):
            for category in StrategyCategory:
                r = _compat(direction, vol, category)
                weakest = min(
                    r.direction_compatibility,
                    r.move_magnitude_compatibility,
                    r.volatility_compatibility,
                    r.payoff_shape_compatibility,
                )
                assert r.overall_semantic_compatibility == weakest


def test_candidates_are_never_hard_filtered_a_contradiction_is_still_a_real_visible_score():
    """Section 9's own explicit rule: even a severe contradiction
    returns a real, visible result -- never None, never an exception."""
    r = _compat(
        DecisionDirection.NEUTRAL,
        DecisionVolatilityView.LONG_VOL,
        StrategyCategory.LONG_CALL_BUTTERFLY,
    )
    assert r is not None
    assert isinstance(r.overall_semantic_compatibility, float)
    assert r.explanation  # a real, non-empty human-readable string
