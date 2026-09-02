"""V4.1 methodology foundation (2026-08-31), Section 20 -- proves V3's
deterministic financial math (analytics/options/payoff.py) is untouched
and produces exactly the same real geometry it always has, using the
REAL frozen legs of all 7 real settled V3 decisions (CRM, VEEV, NVDA, DG,
DY, DLTR, HPQ -- decision_snapshot.legs, queried directly from the real
database as part of the forensic audit and hardcoded here as
deterministic fixtures, never re-queried live).

Nothing in this V4.1 task modifies analytics/options/payoff.py,
analytics/options/strategy_candidates.py, analytics/decision/
strategy_scoring.py, analytics/decision/budget.py,
analytics/decision/risk_profile.py, or services/decision_snapshot_
freezing.py -- this test exists as a permanent, explicit guard against
any FUTURE change to that math silently altering these real, already-
observed historical values, not because this task changed them itself.

Each leg's premium here is decision-time (the real mid-price
decision_snapshot.legs value used for scoring), not the real executable
entry fill -- this test therefore cross-checks analyze()'s own reported
geometry against an INDEPENDENT, hand-computed intrinsic-value payoff at
every real strike, rather than against a different (fill-price-based)
number that would not be expected to match.
"""

from decimal import Decimal

import pytest

from analytics.options.payoff import Action, OptionLeg, analyze
from models.enums import OptionType

# Real decision_snapshot.legs for all 7 real settled V3 decisions
# (ids 4793, 4801, 4802, 4804, 4806, 4810, 4811), each tuple:
# (option_type, action, strike, premium, quantity).
REAL_LEGS: dict[str, list[tuple[OptionType, Action, str, str, int]]] = {
    "DY": [
        (OptionType.CALL, Action.BUY, "370.00", "26.10", 1),
        (OptionType.CALL, Action.SELL, "380.00", "21.70", 2),
        (OptionType.CALL, Action.BUY, "390.00", "18.10", 1),
    ],
    "VEEV": [
        (OptionType.CALL, Action.BUY, "240.00", "18.00", 1),
        (OptionType.CALL, Action.SELL, "250.00", "12.55", 2),
        (OptionType.CALL, Action.BUY, "260.00", "8.75", 1),
    ],
    "CRM": [
        (OptionType.CALL, Action.BUY, "207.50", "7.75", 1),
        (OptionType.CALL, Action.SELL, "210.00", "6.475", 2),
        (OptionType.CALL, Action.BUY, "212.50", "5.425", 1),
    ],
    "HPQ": [
        (OptionType.PUT, Action.BUY, "28.50", "1.06", 1),
        (OptionType.PUT, Action.SELL, "28.00", "0.855", 1),
    ],
    "NVDA": [
        (OptionType.CALL, Action.BUY, "205.00", "9.225", 1),
        (OptionType.CALL, Action.SELL, "207.50", "7.75", 2),
        (OptionType.CALL, Action.BUY, "210.00", "6.45", 1),
    ],
    "DG": [
        (OptionType.PUT, Action.BUY, "123.00", "5.30", 1),
        (OptionType.PUT, Action.SELL, "124.00", "5.725", 1),
        (OptionType.CALL, Action.SELL, "126.00", "3.495", 1),
        (OptionType.CALL, Action.BUY, "127.00", "3.35", 1),
    ],
    "DLTR": [
        (OptionType.CALL, Action.BUY, "136.00", "4.675", 1),
        (OptionType.CALL, Action.SELL, "137.00", "4.175", 2),
        (OptionType.CALL, Action.BUY, "138.00", "3.80", 1),
    ],
}

# Real strategy_direction / real strategy_type for all 7 (decision_snapshot).
REAL_STRATEGY_TYPE = {
    "DY": "long_call_butterfly",
    "VEEV": "long_call_butterfly",
    "CRM": "long_call_butterfly",
    "HPQ": "bear_put_spread",
    "NVDA": "long_call_butterfly",
    "DG": "iron_condor",
    "DLTR": "long_call_butterfly",
}


def _legs(ticker: str) -> list[OptionLeg]:
    return [
        OptionLeg(
            option_type=option_type,
            action=action,
            strike=Decimal(strike),
            premium=Decimal(premium),
            quantity=quantity,
        )
        for option_type, action, strike, premium, quantity in REAL_LEGS[ticker]
    ]


def _independent_intrinsic_payoff(
    legs: list[tuple[OptionType, Action, str, str, int]], underlying: Decimal
) -> Decimal:
    """A second, independently-written intrinsic-value payoff formula --
    deliberately not calling analytics/options/payoff.py at all -- used
    only to cross-check analyze()'s own output at each real leg's strike,
    the textbook definition of a breakeven/extremum point."""
    total = Decimal(0)
    for option_type, action, strike_s, premium_s, quantity in legs:
        strike = Decimal(strike_s)
        premium = Decimal(premium_s)
        if option_type == OptionType.CALL:
            intrinsic = max(underlying - strike, Decimal(0))
        else:
            intrinsic = max(strike - underlying, Decimal(0))
        if action == Action.BUY:
            total += (intrinsic - premium) * quantity
        else:
            total += (premium - intrinsic) * quantity
    return total


@pytest.mark.parametrize("ticker", list(REAL_LEGS.keys()))
def test_real_frozen_legs_reproduce_the_correct_geometry(ticker):
    legs = _legs(ticker)
    analysis = analyze(legs)

    # Independently re-derive max_profit/max_loss by hand-evaluating
    # payoff at every real strike plus S=0 -- the exact set of points
    # analyze()'s own docstring says is sufficient, verified here by a
    # second, separately-written formula rather than trusted blindly.
    strikes = sorted({Decimal(s) for _, _, s, _, _ in REAL_LEGS[ticker]})
    candidate_prices = [Decimal(0), *strikes]
    payoffs = [_independent_intrinsic_payoff(REAL_LEGS[ticker], s) for s in candidate_prices]

    if analysis.max_profit is not None:
        assert analysis.max_profit == max(payoffs)
    if analysis.max_loss is not None:
        assert analysis.max_loss == -min(payoffs)

    # Every real breakeven analyze() reports must itself be a genuine
    # zero of the same independent payoff formula (the actual
    # mathematical definition of "breakeven"), not merely whatever the
    # module happens to currently return.
    for breakeven in analysis.breakevens:
        assert _independent_intrinsic_payoff(REAL_LEGS[ticker], breakeven) == Decimal(0)


def test_all_five_real_butterflies_are_net_debit_structures():
    """The real, observed economic shape every one of V3's 5 real
    butterfly trades actually had -- confirms REAL_LEGS above matches
    the true historical structure this whole V4.1 strategy-semantics
    correction is about."""
    butterflies = [t for t, s in REAL_STRATEGY_TYPE.items() if s == "long_call_butterfly"]
    assert butterflies == ["DY", "VEEV", "CRM", "NVDA", "DLTR"]
    for ticker in butterflies:
        analysis = analyze(_legs(ticker))
        assert analysis.net_premium > 0, f"{ticker} butterfly should be a real net debit"


def test_the_real_iron_condor_dg_is_net_credit():
    analysis = analyze(_legs("DG"))
    assert analysis.net_premium < 0


def test_the_real_bear_put_spread_hpq_is_net_debit():
    analysis = analyze(_legs("HPQ"))
    assert analysis.net_premium > 0
