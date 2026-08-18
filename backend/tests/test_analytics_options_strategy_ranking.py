from datetime import date
from decimal import Decimal

from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from analytics.options.strategy_ranking import rank_strategy_candidates
from models.enums import OptionType

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")
TICKER = "ZZRANK"


def _candidate(category: StrategyCategory, legs: list[OptionLeg]) -> StrategyCandidate:
    return StrategyCandidate(
        category=category,
        legs=tuple(legs),
        analysis=analyze(legs),
        expiration=EXP,
        underlying_price=UNDERLYING,
    )


def _long_call() -> StrategyCandidate:
    # Cheap, small max loss ($2), unbounded upside -- pays off well on an
    # up move, nothing on a down move.
    return _candidate(
        StrategyCategory.LONG_CALL,
        [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("2"))],
    )


def _long_put() -> StrategyCandidate:
    # Symmetric to the call: pays off on a down move, nothing on an up move.
    return _candidate(
        StrategyCategory.LONG_PUT,
        [OptionLeg(OptionType.PUT, Action.BUY, Decimal("100"), Decimal("2"))],
    )


def _long_straddle() -> StrategyCandidate:
    # Pays off on *either* a real up or down move -- more expensive, but
    # should score well against a real implied move regardless of direction.
    return _candidate(
        StrategyCategory.LONG_STRADDLE,
        [
            OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("4")),
            OptionLeg(OptionType.PUT, Action.BUY, Decimal("100"), Decimal("4")),
        ],
    )


def test_ranks_are_1_indexed_and_contiguous():
    ranked = rank_strategy_candidates([_long_call(), _long_put()], TICKER, Decimal("0.10"))
    assert sorted(r.rank for r in ranked) == [1, 2]


def test_a_strictly_cheaper_identical_payoff_always_outranks_a_pricier_one():
    # Same strike, same structure, but B costs $1 more for an identical
    # intrinsic payoff -- B is strictly dominated (worse P&L in *every*
    # price scenario, and worse max loss), so the ranking must never put
    # it ahead of A, at any real implied move.
    cheaper = _candidate(
        StrategyCategory.LONG_CALL,
        [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("2"))],
    )
    pricier = _candidate(
        StrategyCategory.LONG_CALL,
        [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("3"))],
    )
    ranked = rank_strategy_candidates([pricier, cheaper], TICKER, Decimal("0.10"))
    assert ranked[0].candidate.analysis.max_loss == Decimal("2")
    assert ranked[1].candidate.analysis.max_loss == Decimal("3")


def test_scenario_pnl_matches_real_payoff_math():
    ranked = rank_strategy_candidates([_long_call()], TICKER, Decimal("0.10"))
    result = ranked[0]
    assert result.scenario is not None
    # Down 10% to $90: call strike 100, worthless -- lose the $2 premium.
    assert result.scenario.down_price == Decimal("90.0")
    assert result.scenario.down_pnl == Decimal("-2")
    # Up 10% to $110: intrinsic 10, minus $2 premium = $8.
    assert result.scenario.up_price == Decimal("110.0")
    assert result.scenario.up_pnl == Decimal("8")


def test_explanation_cites_real_numbers_not_a_fabricated_confidence_score():
    ranked = rank_strategy_candidates([_long_call()], TICKER, Decimal("0.10"))
    explanation = ranked[0].explanation
    assert TICKER in explanation
    assert "10.0%" in explanation
    assert "$8.00" in explanation  # the real up-move P&L
    assert "$-2.00" in explanation or "-$2.00" in explanation or "$2.00" in explanation


def test_falls_back_to_max_loss_ranking_when_no_implied_move_available():
    cheap = _long_call()  # max_loss = $2
    expensive = _long_straddle()  # max_loss = $8
    ranked = rank_strategy_candidates([expensive, cheap], TICKER, None)
    assert [r.candidate.category for r in ranked] == [
        StrategyCategory.LONG_CALL,
        StrategyCategory.LONG_STRADDLE,
    ]
    assert all(r.scenario is None for r in ranked)


def test_fallback_explanation_is_honest_about_missing_implied_move():
    ranked = rank_strategy_candidates([_long_call()], TICKER, None)
    assert "No real options-implied move is on record" in ranked[0].explanation


def test_empty_candidate_list_ranks_to_empty_list():
    assert rank_strategy_candidates([], TICKER, Decimal("0.05")) == []
