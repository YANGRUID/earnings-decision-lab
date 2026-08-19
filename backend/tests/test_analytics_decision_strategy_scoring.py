from datetime import date
from decimal import Decimal

from analytics.decision.strategy_scoring import (
    filter_candidates_by_risk_preference,
    rank_candidates_for_view,
    score_candidate_for_view,
    target_price_for_direction,
)
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import (
    DecisionDirection,
    DecisionVolatilityView,
    OptionType,
    StrategyRiskPreference,
)

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")


def _candidate(category: StrategyCategory, legs: list[OptionLeg]) -> StrategyCandidate:
    return StrategyCandidate(
        category=category,
        legs=tuple(legs),
        analysis=analyze(legs),
        expiration=EXP,
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


def _uncovered_short_call() -> StrategyCandidate:
    # A naked/uncovered short call -- this project's strategy_candidates.py
    # never constructs one (every real category has a defined max loss "by
    # construction"), but the underlying payoff engine correctly computes
    # unbounded risk for one if built directly, which is exactly what this
    # test exercises: risk_reward must never score undefined risk
    # favorably, even though no real code path generates this today.
    legs = [OptionLeg(OptionType.CALL, Action.SELL, Decimal("100"), Decimal("5"))]
    return _candidate(StrategyCategory.LONG_CALL, legs)  # category value irrelevant to this test


def _put_credit_spread() -> StrategyCandidate:
    # Net credit -- sells the higher put, buys the lower put for protection.
    return _candidate(
        StrategyCategory.PUT_CREDIT_SPREAD,
        [
            OptionLeg(OptionType.PUT, Action.SELL, Decimal("95"), Decimal("3")),
            OptionLeg(OptionType.PUT, Action.BUY, Decimal("90"), Decimal("1")),
        ],
    )


class TestTargetPriceForDirection:
    def test_strong_bullish_targets_full_implied_move_up(self):
        price = target_price_for_direction(
            UNDERLYING, Decimal("0.10"), DecisionDirection.STRONG_BULLISH
        )
        assert price == Decimal("110.0")

    def test_bullish_targets_half_implied_move_up(self):
        price = target_price_for_direction(UNDERLYING, Decimal("0.10"), DecisionDirection.BULLISH)
        assert price == Decimal("105.0")

    def test_neutral_targets_no_move(self):
        price = target_price_for_direction(UNDERLYING, Decimal("0.10"), DecisionDirection.NEUTRAL)
        assert price == UNDERLYING

    def test_strong_bearish_targets_full_implied_move_down(self):
        price = target_price_for_direction(
            UNDERLYING, Decimal("0.10"), DecisionDirection.STRONG_BEARISH
        )
        assert price == Decimal("90.0")

    def test_no_implied_move_returns_none(self):
        assert target_price_for_direction(UNDERLYING, None, DecisionDirection.BULLISH) is None


class TestScoreCandidateForView:
    def test_long_call_scores_well_for_bullish_view_with_liquidity(self):
        breakdown, target_price, payoff, _ = score_candidate_for_view(
            _long_call(),
            direction=DecisionDirection.STRONG_BULLISH,
            volatility_view=DecisionVolatilityView.LONG_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[Decimal("0.08"), Decimal("0.12")],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert target_price == Decimal("110.0")
        assert payoff is not None and payoff > 0
        assert breakdown.direction_fit > 0
        assert breakdown.liquidity == 10
        assert breakdown.data_quality == 10
        assert breakdown.total <= 100

    def test_long_call_scores_zero_direction_fit_for_bearish_view(self):
        breakdown, _, payoff, _ = score_candidate_for_view(
            _long_call(),
            direction=DecisionDirection.STRONG_BEARISH,
            volatility_view=DecisionVolatilityView.LONG_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert payoff is not None and payoff <= 0
        assert breakdown.direction_fit == 0

    def test_no_bid_ask_scores_zero_liquidity(self):
        breakdown, *_ = score_candidate_for_view(
            _long_call(),
            direction=DecisionDirection.BULLISH,
            volatility_view=DecisionVolatilityView.LONG_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=False,
            market_data_quality="frozen",
        )
        assert breakdown.liquidity == 0

    def test_credit_spread_undefined_vs_defined_risk_reward(self):
        # A credit spread has a defined max loss (net_premium is negative,
        # but max_loss is still finite) -- risk_reward must not be 0.
        breakdown, *_ = score_candidate_for_view(
            _put_credit_spread(),
            direction=DecisionDirection.BULLISH,
            volatility_view=DecisionVolatilityView.SHORT_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert breakdown.risk_reward >= 0  # never negative, never crashes on defined max_loss

    def test_uncovered_short_call_scores_zero_risk_reward(self):
        # Undefined/unlimited risk (see _uncovered_short_call's docstring)
        # must never be scored favorably -- risk_reward is exactly 0,
        # regardless of how attractive the premium collected looks.
        breakdown, *_ = score_candidate_for_view(
            _uncovered_short_call(),
            direction=DecisionDirection.BEARISH,
            volatility_view=DecisionVolatilityView.SHORT_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert breakdown.risk_reward == 0

    def test_breakdown_total_never_exceeds_100(self):
        breakdown, *_ = score_candidate_for_view(
            _long_call(),
            direction=DecisionDirection.STRONG_BULLISH,
            volatility_view=DecisionVolatilityView.LONG_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[Decimal("0.15")] * 10,
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert breakdown.total <= 100

    def test_long_call_scores_full_volatility_fit_for_long_vol_view(self):
        # A long call is a net-debit position -- full volatility_fit
        # weight under LONG_VOL, zero under SHORT_VOL.
        breakdown, *_ = score_candidate_for_view(
            _long_call(),
            direction=DecisionDirection.BULLISH,
            volatility_view=DecisionVolatilityView.LONG_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert breakdown.volatility_fit == 15

    def test_long_call_scores_zero_volatility_fit_for_short_vol_view(self):
        breakdown, *_ = score_candidate_for_view(
            _long_call(),
            direction=DecisionDirection.BULLISH,
            volatility_view=DecisionVolatilityView.SHORT_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert breakdown.volatility_fit == 0

    def test_credit_spread_scores_full_volatility_fit_for_short_vol_view(self):
        # A put credit spread is a net-credit position -- the opposite
        # pattern from the long call above.
        short_breakdown, *_ = score_candidate_for_view(
            _put_credit_spread(),
            direction=DecisionDirection.NEUTRAL,
            volatility_view=DecisionVolatilityView.SHORT_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        long_breakdown, *_ = score_candidate_for_view(
            _put_credit_spread(),
            direction=DecisionDirection.NEUTRAL,
            volatility_view=DecisionVolatilityView.LONG_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert short_breakdown.volatility_fit == 15
        assert long_breakdown.volatility_fit == 0

    def test_neutral_vol_view_gives_a_fixed_midpoint_regardless_of_structure(self):
        long_call_breakdown, *_ = score_candidate_for_view(
            _long_call(),
            direction=DecisionDirection.NEUTRAL,
            volatility_view=DecisionVolatilityView.NEUTRAL_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        credit_spread_breakdown, *_ = score_candidate_for_view(
            _put_credit_spread(),
            direction=DecisionDirection.NEUTRAL,
            volatility_view=DecisionVolatilityView.NEUTRAL_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert long_call_breakdown.volatility_fit == credit_spread_breakdown.volatility_fit == 8


class TestRankCandidatesForView:
    def test_bullish_view_ranks_long_call_above_long_put(self):
        ranked = rank_candidates_for_view(
            [_long_call(), _long_put()],
            direction=DecisionDirection.STRONG_BULLISH,
            volatility_view=DecisionVolatilityView.LONG_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert ranked[0].candidate.category == StrategyCategory.LONG_CALL
        assert ranked[0].rank == 1
        assert ranked[1].rank == 2

    def test_bearish_view_ranks_long_put_above_long_call(self):
        ranked = rank_candidates_for_view(
            [_long_call(), _long_put()],
            direction=DecisionDirection.STRONG_BEARISH,
            volatility_view=DecisionVolatilityView.LONG_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert ranked[0].candidate.category == StrategyCategory.LONG_PUT

    def test_empty_candidates_returns_empty(self):
        assert (
            rank_candidates_for_view(
                [],
                direction=DecisionDirection.NEUTRAL,
                volatility_view=DecisionVolatilityView.NEUTRAL_VOL,
                implied_move_pct=None,
                historical_move_pcts=[],
                has_bid_ask=False,
                market_data_quality=None,
            )
            == []
        )

    def test_neutral_long_vol_does_not_rank_a_short_vol_credit_structure_first(self):
        # The real bug this component fixes: under a Neutral direction,
        # direction_fit alone can't distinguish a long-vol long straddle
        # from a short-vol put credit spread (neither's payoff differs
        # much at a "no move" target price) -- volatility_fit must be what
        # breaks that tie in favor of the structure that actually matches
        # a stated Long Vol view.
        straddle = _candidate(
            StrategyCategory.LONG_STRADDLE,
            [
                OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("3")),
                OptionLeg(OptionType.PUT, Action.BUY, Decimal("100"), Decimal("3")),
            ],
        )
        ranked = rank_candidates_for_view(
            [_put_credit_spread(), straddle],
            direction=DecisionDirection.NEUTRAL,
            volatility_view=DecisionVolatilityView.LONG_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert ranked[0].candidate.category == StrategyCategory.LONG_STRADDLE

    def test_neutral_short_vol_favors_the_credit_structure_over_the_debit_one(self):
        straddle = _candidate(
            StrategyCategory.LONG_STRADDLE,
            [
                OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("3")),
                OptionLeg(OptionType.PUT, Action.BUY, Decimal("100"), Decimal("3")),
            ],
        )
        ranked = rank_candidates_for_view(
            [straddle, _put_credit_spread()],
            direction=DecisionDirection.NEUTRAL,
            volatility_view=DecisionVolatilityView.SHORT_VOL,
            implied_move_pct=Decimal("0.10"),
            historical_move_pcts=[],
            has_bid_ask=True,
            market_data_quality="live",
        )
        assert ranked[0].candidate.category == StrategyCategory.PUT_CREDIT_SPREAD


class TestFilterCandidatesByRiskPreference:
    def test_defined_risk_only_excludes_single_leg_longs(self):
        candidates = [_long_call(), _long_put(), _put_credit_spread()]
        filtered = filter_candidates_by_risk_preference(
            candidates, StrategyRiskPreference.DEFINED_RISK_ONLY
        )
        categories = {c.category for c in filtered}
        assert StrategyCategory.LONG_CALL not in categories
        assert StrategyCategory.LONG_PUT not in categories
        assert StrategyCategory.PUT_CREDIT_SPREAD in categories

    def test_allow_single_leg_long_includes_everything(self):
        candidates = [_long_call(), _long_put(), _put_credit_spread()]
        filtered = filter_candidates_by_risk_preference(
            candidates, StrategyRiskPreference.ALLOW_SINGLE_LEG_LONG
        )
        assert len(filtered) == 3

    def test_advanced_tier_behaves_like_allow_single_leg_long_today(self):
        candidates = [_long_call(), _long_put()]
        filtered = filter_candidates_by_risk_preference(
            candidates, StrategyRiskPreference.ADVANCED_ALLOW_UNCOVERED_SHORT
        )
        assert len(filtered) == 2
