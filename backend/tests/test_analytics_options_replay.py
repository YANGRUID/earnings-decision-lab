from decimal import Decimal

import pytest

from analytics.options import strategies
from analytics.options.replay import (
    StrikeSelectionRule,
    build_replay,
    nearest_available_strike,
    select_strike,
)

D = Decimal

# Clearly-synthetic strike ladder for testing — not real market data.
STRIKES = {D(s) for s in (90, 95, 100, 105, 110, 115, 120)}


def test_nearest_available_strike():
    assert nearest_available_strike(STRIKES, D("103")) == D(105)
    assert nearest_available_strike(STRIKES, D("102")) == D(100)


def test_nearest_available_strike_rejects_empty_set():
    with pytest.raises(ValueError):
        nearest_available_strike(set(), D("100"))


def test_select_strike_nearest_atm():
    strike = select_strike(StrikeSelectionRule.NEAREST_ATM, STRIKES, underlying_price=D("103"))
    assert strike == D(105)


def test_select_strike_fixed_pct_otm_call_direction_up():
    # 10% OTM call target from 100 -> 110; nearest available is exactly 110.
    strike = select_strike(
        StrikeSelectionRule.FIXED_PCT_OTM,
        STRIKES,
        underlying_price=D("100"),
        otm_pct=D("0.10"),
        direction="up",
    )
    assert strike == D(110)


def test_select_strike_fixed_pct_otm_put_direction_down():
    strike = select_strike(
        StrikeSelectionRule.FIXED_PCT_OTM,
        STRIKES,
        underlying_price=D("100"),
        otm_pct=D("0.10"),
        direction="down",
    )
    assert strike == D(90)


def test_select_strike_fixed_pct_otm_requires_direction():
    with pytest.raises(ValueError):
        select_strike(
            StrikeSelectionRule.FIXED_PCT_OTM, STRIKES, underlying_price=D("100"), otm_pct=D("0.1")
        )


def test_select_strike_nearest_to_target():
    strike = select_strike(
        StrikeSelectionRule.NEAREST_TO_TARGET, STRIKES, underlying_price=D("100"), target=D("117")
    )
    assert strike == D(115)


def test_select_strike_nearest_to_target_requires_target():
    with pytest.raises(ValueError):
        select_strike(StrikeSelectionRule.NEAREST_TO_TARGET, STRIKES, underlying_price=D("100"))


def test_build_replay_long_call_with_evaluation():
    legs = strategies.long_call(D(105), D("4.20"))
    result = build_replay(
        strategy_name="long_call",
        strike_selection_rule=StrikeSelectionRule.NEAREST_ATM,
        legs=legs,
        underlying_price_at_entry=D("103"),
        underlying_price_at_evaluation=D("118"),
    )

    assert result.net_premium == D("4.20")
    assert result.max_loss == D("4.20")
    assert result.max_profit is None  # unbounded
    assert result.breakevens == (D("109.20"),)
    assert result.payoff_at_evaluation == D("118") - D(105) - D("4.20")


def test_build_replay_without_evaluation_leaves_payoff_none():
    legs = strategies.long_put(D(100), D(3))
    result = build_replay(
        strategy_name="long_put",
        strike_selection_rule=StrikeSelectionRule.NEAREST_ATM,
        legs=legs,
        underlying_price_at_entry=D("101"),
    )

    assert result.underlying_price_at_evaluation is None
    assert result.payoff_at_evaluation is None
    assert result.max_profit == D(97)


def test_build_replay_iron_condor_uses_rule_selected_strikes():
    put_long = select_strike(StrikeSelectionRule.NEAREST_TO_TARGET, STRIKES, D(100), target=D(90))
    put_short = select_strike(StrikeSelectionRule.NEAREST_TO_TARGET, STRIKES, D(100), target=D(95))
    call_short = select_strike(
        StrikeSelectionRule.NEAREST_TO_TARGET, STRIKES, D(100), target=D(105)
    )
    call_long = select_strike(StrikeSelectionRule.NEAREST_TO_TARGET, STRIKES, D(100), target=D(110))

    legs = strategies.iron_condor(
        put_long_strike=put_long,
        put_long_premium=D(1),
        put_short_strike=put_short,
        put_short_premium=D(2),
        call_short_strike=call_short,
        call_short_premium=D(2),
        call_long_strike=call_long,
        call_long_premium=D(1),
    )
    result = build_replay(
        strategy_name="iron_condor",
        strike_selection_rule=StrikeSelectionRule.NEAREST_TO_TARGET,
        legs=legs,
        underlying_price_at_entry=D(100),
    )

    assert result.max_profit == D(2)
    assert result.max_loss == D(3)
