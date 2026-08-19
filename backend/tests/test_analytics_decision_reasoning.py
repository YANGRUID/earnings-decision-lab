from datetime import date
from decimal import Decimal

from analytics.decision.reasoning import (
    build_expiration_bullets,
    build_risk_bullets,
    build_risk_profile_fit_bullets,
    build_strike_bullets,
    build_why_bullets,
    build_why_not_alternative_bullets,
    strategy_category_label,
)
from analytics.decision.strategy_scoring import ViewRankedStrategy, score_candidate_for_view
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import DecisionDirection, DecisionVolatilityView, OptionType, RiskProfile

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")


def _long_call() -> StrategyCandidate:
    legs = [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("2"))]
    return StrategyCandidate(
        category=StrategyCategory.LONG_CALL,
        legs=tuple(legs),
        analysis=analyze(legs),
        expiration=EXP,
        underlying_price=UNDERLYING,
    )


def _ranked(candidate: StrategyCandidate, direction: DecisionDirection) -> ViewRankedStrategy:
    breakdown, target_price, payoff, compat = score_candidate_for_view(
        candidate,
        direction=direction,
        volatility_view=DecisionVolatilityView.NEUTRAL_VOL,
        implied_move_pct=Decimal("0.10"),
        historical_move_pcts=[Decimal("0.08"), Decimal("0.12"), Decimal("0.03")],
        has_bid_ask=True,
        market_data_quality="live",
    )
    return ViewRankedStrategy(
        candidate=candidate,
        rank=1,
        target_price=target_price,
        payoff_at_target=payoff,
        score=breakdown,
        move_compatibility=compat,
    )


def test_why_bullets_contain_real_numbers_not_placeholders():
    ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
    bullets = build_why_bullets(
        ranked,
        direction=DecisionDirection.BULLISH,
        implied_move_pct=Decimal("0.10"),
        has_bid_ask=True,
    )
    joined = " ".join(bullets)
    assert "moderately bullish" in joined
    assert "±10.0%" in joined
    assert "Risk is capped at $2.00" in joined
    assert any("real bid/ask" in b.lower() for b in bullets)


def test_why_bullets_note_missing_liquidity_honestly():
    ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
    bullets = build_why_bullets(
        ranked,
        direction=DecisionDirection.BULLISH,
        implied_move_pct=Decimal("0.10"),
        has_bid_ask=False,
    )
    assert any("not available" in b.lower() for b in bullets)


def test_risk_bullets_flag_debit_loss_and_iv_crush():
    ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
    bullets = build_risk_bullets(ranked)
    joined = " ".join(bullets).lower()
    assert "paid to enter" in joined
    assert "iv crush" in joined


def test_risk_bullets_flag_small_sample_size():
    candidate = _long_call()
    breakdown, target_price, payoff, compat = score_candidate_for_view(
        candidate,
        direction=DecisionDirection.BULLISH,
        volatility_view=DecisionVolatilityView.NEUTRAL_VOL,
        implied_move_pct=Decimal("0.10"),
        historical_move_pcts=[Decimal("0.08")],  # only 1 -- small sample
        has_bid_ask=True,
        market_data_quality="live",
    )
    ranked = ViewRankedStrategy(
        candidate=candidate,
        rank=1,
        target_price=target_price,
        payoff_at_target=payoff,
        score=breakdown,
        move_compatibility=compat,
    )
    bullets = build_risk_bullets(ranked)
    assert any("small sample" in b.lower() for b in bullets)


def test_strategy_category_label_is_readable():
    assert strategy_category_label(_long_call()) == "Long Call"


def _put_credit_spread() -> StrategyCandidate:
    # Net credit of $0.50 -- deliberately different in magnitude from the
    # long call's $2.00 debit so cost-comparison bullets have something
    # real to report.
    legs = [
        OptionLeg(OptionType.PUT, Action.SELL, Decimal("95"), Decimal("1.5")),
        OptionLeg(OptionType.PUT, Action.BUY, Decimal("90"), Decimal("1")),
    ]
    return StrategyCandidate(
        category=StrategyCategory.PUT_CREDIT_SPREAD,
        legs=tuple(legs),
        analysis=analyze(legs),
        expiration=EXP,
        underlying_price=UNDERLYING,
    )


class TestBuildExpirationBullets:
    def test_sweet_spot_dte_is_described(self):
        ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
        bullets = build_expiration_bullets(ranked, date(2026, 9, 8))  # 10 days before EXP
        joined = " ".join(bullets)
        assert "10 day(s)" in joined
        assert "7-21 day window" in joined

    def test_pre_earnings_expiration_is_flagged(self):
        ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
        bullets = build_expiration_bullets(ranked, date(2026, 9, 20))  # after EXP
        assert any("does not cover the event" in b for b in bullets)

    def test_no_earnings_date_is_labeled_unanchored(self):
        ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
        bullets = build_expiration_bullets(ranked, None)
        assert any("not anchored" in b for b in bullets)


class TestBuildStrikeBullets:
    def test_describes_each_real_leg(self):
        ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
        bullets = build_strike_bullets(ranked)
        joined = " ".join(bullets)
        assert "$100.00" in joined
        assert "call" in joined.lower()

    def test_describes_nearest_breakeven(self):
        ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
        bullets = build_strike_bullets(ranked)
        assert any("breakeven" in b.lower() for b in bullets)


class TestBuildRiskProfileFitBullets:
    def test_none_profile_returns_empty(self):
        ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
        assert build_risk_profile_fit_bullets(ranked, None) == []

    def test_conservative_notes_exclusion_of_single_leg_long(self):
        ranked = _ranked(_put_credit_spread(), DecisionDirection.NEUTRAL)
        bullets = build_risk_profile_fit_bullets(ranked, RiskProfile.CONSERVATIVE)
        assert any("excludes single-leg long" in b for b in bullets)

    def test_aggressive_favors_single_leg_long(self):
        ranked = _ranked(_long_call(), DecisionDirection.BULLISH)
        bullets = build_risk_profile_fit_bullets(ranked, RiskProfile.AGGRESSIVE)
        assert any("favors single-leg long" in b for b in bullets)

    def test_aggressive_spread_has_no_favoring_bullet(self):
        ranked = _ranked(_put_credit_spread(), DecisionDirection.NEUTRAL)
        bullets = build_risk_profile_fit_bullets(ranked, RiskProfile.AGGRESSIVE)
        assert not any("favors single-leg long" in b for b in bullets)


class TestBuildWhyNotAlternativeBullets:
    def test_real_cost_difference_is_cited(self):
        top = _ranked(_long_call(), DecisionDirection.BULLISH)
        alt = _ranked(_put_credit_spread(), DecisionDirection.NEUTRAL)
        bullets = build_why_not_alternative_bullets(top, alt)
        joined = " ".join(bullets)
        assert "Put Credit Spread" in joined
        assert "premium" in joined.lower()

    def test_undefined_risk_alternative_is_flagged(self):
        legs = [OptionLeg(OptionType.CALL, Action.SELL, Decimal("100"), Decimal("5"))]
        uncovered = StrategyCandidate(
            category=StrategyCategory.LONG_CALL,  # category irrelevant to this test
            legs=tuple(legs),
            analysis=analyze(legs),
            expiration=EXP,
            underlying_price=UNDERLYING,
        )
        top = _ranked(_long_call(), DecisionDirection.BULLISH)
        alt = _ranked(uncovered, DecisionDirection.BULLISH)
        bullets = build_why_not_alternative_bullets(top, alt)
        assert any("undefined/unbounded risk" in b for b in bullets)

    def test_score_gap_cites_the_biggest_driving_component(self):
        top = _ranked(_long_call(), DecisionDirection.STRONG_BULLISH)
        alt = _ranked(_put_credit_spread(), DecisionDirection.STRONG_BULLISH)
        bullets = build_why_not_alternative_bullets(top, alt)
        assert any("total score points" in b for b in bullets)
