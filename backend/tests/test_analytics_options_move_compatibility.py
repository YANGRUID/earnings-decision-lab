from datetime import date
from decimal import Decimal

from analytics.options.move_compatibility import assess_move_compatibility
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import OptionType

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


def _long_call(premium: Decimal = Decimal("2")) -> StrategyCandidate:
    # Breakeven at strike + premium = 102 -> required move = 2%.
    return _candidate(
        StrategyCategory.LONG_CALL,
        [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), premium)],
    )


def _iron_condor() -> StrategyCandidate:
    # Short strikes at 95/105 (5% each way), wings at 90/110, net credit.
    legs = [
        OptionLeg(OptionType.PUT, Action.BUY, Decimal("90"), Decimal("0.50")),
        OptionLeg(OptionType.PUT, Action.SELL, Decimal("95"), Decimal("1.50")),
        OptionLeg(OptionType.CALL, Action.SELL, Decimal("105"), Decimal("1.50")),
        OptionLeg(OptionType.CALL, Action.BUY, Decimal("110"), Decimal("0.50")),
    ]
    return _candidate(StrategyCategory.IRON_CONDOR, legs)


def test_none_when_no_historical_moves():
    result = assess_move_compatibility(_long_call(), [])
    assert result is None


def test_debit_strategy_counts_moves_at_or_beyond_the_breakeven():
    # Long call breakeven requires a >= 2% move to profit.
    moves = [Decimal("0.01"), Decimal("0.02"), Decimal("0.05"), Decimal("-0.10")]
    result = assess_move_compatibility(_long_call(), moves)
    assert result is not None
    assert result.method == "historical_move_compatibility"
    assert result.requires_move_beyond_threshold is True
    assert result.required_move_pct == Decimal("0.02")
    # 0.02, 0.05, -0.10 all have |move| >= 0.02 -> 3 compatible out of 4.
    assert result.compatible_count == 3
    assert result.sample_size == 4
    assert result.compatible_pct == Decimal("3") / Decimal("4")


def test_credit_strategy_counts_moves_that_stay_inside_the_breakeven():
    condor = _iron_condor()
    assert condor.analysis.net_premium < 0  # sanity: this really is a credit
    required = condor.analysis.breakevens and min(
        abs(be - UNDERLYING) / UNDERLYING for be in condor.analysis.breakevens
    )
    moves = [Decimal("0.01"), Decimal("-0.02"), required + Decimal("0.01"), Decimal("0.20")]
    result = assess_move_compatibility(condor, moves)
    assert result is not None
    assert result.requires_move_beyond_threshold is False
    # Only the two small moves stay strictly inside the threshold.
    assert result.compatible_count == 2


def test_historical_moves_are_carried_through_unmodified_for_audit():
    moves = [Decimal("0.03"), Decimal("-0.07")]
    result = assess_move_compatibility(_long_call(), moves)
    assert result is not None
    assert result.historical_moves_pct == tuple(moves)


def test_none_when_candidate_has_no_breakevens():
    # Constructed directly (not via generate_candidates) to exercise the
    # defensive guard, even though the real generator never produces a
    # candidate with zero breakevens for any of the required categories.
    from dataclasses import replace

    candidate = _long_call()
    candidate = replace(candidate, analysis=replace(candidate.analysis, breakevens=()))

    result = assess_move_compatibility(candidate, [Decimal("0.05")])
    assert result is None
