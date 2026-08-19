from datetime import date
from decimal import Decimal

from analytics.decision.risk_profile import (
    DEFAULT_MAX_RISK_UTILIZATION_PCT,
    MIN_BID_ASK_COVERAGE,
    default_max_risk_utilization_pct,
    default_risk_profile_from_preference,
    filter_candidates_by_risk_profile,
    meets_liquidity_gate,
)
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import OptionType, RiskProfile, StrategyRiskPreference

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")


def _candidate(category: StrategyCategory, legs: list[OptionLeg]) -> StrategyCandidate:
    return StrategyCandidate(
        category=category, legs=tuple(legs), analysis=analyze(legs), expiration=EXP,
        underlying_price=UNDERLYING,
    )


def _long_call() -> StrategyCandidate:
    return _candidate(
        StrategyCategory.LONG_CALL,
        [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("2"))],
    )


def _long_put() -> StrategyCandidate:
    return _candidate(
        StrategyCategory.LONG_PUT,
        [OptionLeg(OptionType.PUT, Action.BUY, Decimal("100"), Decimal("2"))],
    )


def _bear_put_spread() -> StrategyCandidate:
    return _candidate(
        StrategyCategory.BEAR_PUT_SPREAD,
        [
            OptionLeg(OptionType.PUT, Action.BUY, Decimal("100"), Decimal("3")),
            OptionLeg(OptionType.PUT, Action.SELL, Decimal("95"), Decimal("1")),
        ],
    )


class TestFilterCandidatesByRiskProfile:
    def test_conservative_excludes_single_leg_long(self):
        candidates = [_long_call(), _long_put(), _bear_put_spread()]
        filtered = filter_candidates_by_risk_profile(candidates, RiskProfile.CONSERVATIVE)
        assert filtered == [candidates[2]]

    def test_moderate_allows_single_leg_long(self):
        candidates = [_long_call(), _long_put(), _bear_put_spread()]
        filtered = filter_candidates_by_risk_profile(candidates, RiskProfile.MODERATE)
        assert filtered == candidates

    def test_aggressive_allows_single_leg_long(self):
        candidates = [_long_call(), _long_put(), _bear_put_spread()]
        filtered = filter_candidates_by_risk_profile(candidates, RiskProfile.AGGRESSIVE)
        assert filtered == candidates

    def test_empty_input_returns_empty(self):
        assert filter_candidates_by_risk_profile([], RiskProfile.CONSERVATIVE) == []


class TestMeetsLiquidityGate:
    def test_conservative_requires_high_coverage(self):
        assert meets_liquidity_gate(RiskProfile.CONSERVATIVE, 18, 22) is True  # ~82%
        assert meets_liquidity_gate(RiskProfile.CONSERVATIVE, 10, 22) is False  # ~45%

    def test_moderate_requires_moderate_coverage(self):
        assert meets_liquidity_gate(RiskProfile.MODERATE, 10, 22) is True  # ~45%
        assert meets_liquidity_gate(RiskProfile.MODERATE, 5, 22) is False  # ~23%

    def test_aggressive_has_no_extra_floor(self):
        assert meets_liquidity_gate(RiskProfile.AGGRESSIVE, 1, 22) is True
        assert meets_liquidity_gate(RiskProfile.AGGRESSIVE, 0, 22) is True

    def test_zero_contracts_fails_every_gated_profile(self):
        assert meets_liquidity_gate(RiskProfile.CONSERVATIVE, 0, 0) is False
        assert meets_liquidity_gate(RiskProfile.MODERATE, 0, 0) is False
        # AGGRESSIVE has no threshold at all, so it's exempt even at 0/0.
        assert meets_liquidity_gate(RiskProfile.AGGRESSIVE, 0, 0) is True

    def test_exact_threshold_boundary_passes(self):
        # Conservative threshold is exactly 0.80.
        assert meets_liquidity_gate(RiskProfile.CONSERVATIVE, 16, 20) is True


class TestDefaultMaxRiskUtilizationPct:
    def test_ordering_is_strictly_increasing(self):
        conservative = default_max_risk_utilization_pct(RiskProfile.CONSERVATIVE)
        moderate = default_max_risk_utilization_pct(RiskProfile.MODERATE)
        aggressive = default_max_risk_utilization_pct(RiskProfile.AGGRESSIVE)
        assert conservative < moderate < aggressive

    def test_within_documented_bands(self):
        conservative = DEFAULT_MAX_RISK_UTILIZATION_PCT[RiskProfile.CONSERVATIVE]
        moderate = DEFAULT_MAX_RISK_UTILIZATION_PCT[RiskProfile.MODERATE]
        aggressive = DEFAULT_MAX_RISK_UTILIZATION_PCT[RiskProfile.AGGRESSIVE]
        assert Decimal("10") <= conservative <= Decimal("20")
        assert Decimal("20") <= moderate <= Decimal("35")
        assert Decimal("35") <= aggressive <= Decimal("60")


class TestDefaultRiskProfileFromPreference:
    def test_defined_risk_only_maps_to_conservative(self):
        assert (
            default_risk_profile_from_preference(StrategyRiskPreference.DEFINED_RISK_ONLY)
            == RiskProfile.CONSERVATIVE
        )

    def test_allow_single_leg_long_maps_to_moderate(self):
        assert (
            default_risk_profile_from_preference(StrategyRiskPreference.ALLOW_SINGLE_LEG_LONG)
            == RiskProfile.MODERATE
        )

    def test_advanced_maps_to_aggressive(self):
        assert (
            default_risk_profile_from_preference(
                StrategyRiskPreference.ADVANCED_ALLOW_UNCOVERED_SHORT
            )
            == RiskProfile.AGGRESSIVE
        )

    def test_every_preference_has_a_mapping(self):
        for pref in StrategyRiskPreference:
            assert isinstance(default_risk_profile_from_preference(pref), RiskProfile)


class TestMinBidAskCoverageThresholds:
    def test_conservative_stricter_than_moderate(self):
        assert MIN_BID_ASK_COVERAGE[RiskProfile.CONSERVATIVE] > MIN_BID_ASK_COVERAGE[
            RiskProfile.MODERATE
        ]

    def test_aggressive_has_no_threshold(self):
        assert MIN_BID_ASK_COVERAGE[RiskProfile.AGGRESSIVE] is None
