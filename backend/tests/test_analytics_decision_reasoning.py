from datetime import date
from decimal import Decimal

from analytics.decision.reasoning import (
    build_risk_bullets,
    build_why_bullets,
    strategy_category_label,
)
from analytics.decision.strategy_scoring import ViewRankedStrategy, score_candidate_for_view
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import DecisionDirection, DecisionVolatilityView, OptionType

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
