from decimal import Decimal

import pytest

from analytics.options import strategies
from analytics.options.payoff import Action, OptionLeg, analyze, total_payoff
from models.enums import OptionType

D = Decimal


def test_leg_rejects_invalid_construction():
    with pytest.raises(ValueError):
        OptionLeg(OptionType.CALL, Action.BUY, D(-100), D(5))
    with pytest.raises(ValueError):
        OptionLeg(OptionType.CALL, Action.BUY, D(100), D(-5))
    with pytest.raises(ValueError):
        OptionLeg(OptionType.CALL, Action.BUY, D(100), D(5), quantity=0)


def test_long_call():
    legs = strategies.long_call(D(100), D(5))
    result = analyze(legs)

    assert result.net_premium == D(5)
    assert result.max_loss == D(5)
    assert result.max_profit is None  # unbounded upside
    assert result.breakevens == (D(105),)
    assert total_payoff(legs, D(90)) == D(-5)
    assert total_payoff(legs, D(110)) == D(5)


def test_long_put():
    legs = strategies.long_put(D(100), D(4))
    result = analyze(legs)

    assert result.max_loss == D(4)
    assert result.max_profit == D(96)  # bounded: strike - premium
    assert result.breakevens == (D(96),)
    assert total_payoff(legs, D(0)) == D(96)


def test_bull_call_spread():
    legs = strategies.bull_call_spread(D(100), D(6), D(110), D(2))
    result = analyze(legs)

    assert result.net_premium == D(4)
    assert result.max_loss == D(4)
    assert result.max_profit == D(6)  # (110-100) - 4
    assert result.breakevens == (D(104),)
    assert result.return_on_risk == D(6) / D(4)


def test_bull_call_spread_rejects_wrong_strike_order():
    with pytest.raises(ValueError):
        strategies.bull_call_spread(D(110), D(2), D(100), D(6))


def test_bear_put_spread():
    legs = strategies.bear_put_spread(D(110), D(8), D(100), D(3))
    result = analyze(legs)

    assert result.net_premium == D(5)
    assert result.max_profit == D(5)  # (110-100) - 5
    assert result.max_loss == D(5)
    assert result.breakevens == (D(105),)


def test_put_credit_spread():
    legs = strategies.put_credit_spread(D(100), D(3), D(90), D(1))
    result = analyze(legs)

    assert result.net_premium == D(-2)  # net credit
    assert result.max_profit == D(2)
    assert result.max_loss == D(8)  # (100-90) - 2
    assert result.breakevens == (D(98),)


def test_call_credit_spread():
    legs = strategies.call_credit_spread(D(100), D(4), D(110), D("1.5"))
    result = analyze(legs)

    assert result.net_premium == D("-2.5")
    assert result.max_profit == D("2.5")
    assert result.max_loss == D("7.5")
    assert result.breakevens == (D("102.5"),)


def test_long_straddle():
    legs = strategies.long_straddle(D(100), D(6), D(5))
    result = analyze(legs)

    assert result.net_premium == D(11)
    assert result.max_loss == D(11)
    assert result.max_profit is None
    assert result.breakevens == (D(89), D(111))


def test_long_strangle():
    legs = strategies.long_strangle(D(90), D(2), D(110), D(3))
    result = analyze(legs)

    assert result.net_premium == D(5)
    assert result.max_loss == D(5)
    assert result.max_profit is None
    assert result.breakevens == (D(85), D(115))


def test_long_strangle_rejects_wrong_strike_order():
    with pytest.raises(ValueError):
        strategies.long_strangle(D(110), D(3), D(90), D(2))


def test_iron_condor():
    legs = strategies.iron_condor(
        put_long_strike=D(90),
        put_long_premium=D(1),
        put_short_strike=D(95),
        put_short_premium=D(2),
        call_short_strike=D(105),
        call_short_premium=D(2),
        call_long_strike=D(110),
        call_long_premium=D(1),
    )
    result = analyze(legs)

    assert result.net_premium == D(-2)  # net credit received
    assert result.max_profit == D(2)
    assert result.max_loss == D(3)  # wing width (5) - credit (2)
    assert result.breakevens == (D(93), D(107))
    assert total_payoff(legs, D(100)) == D(2)  # max profit region, between short strikes
    assert total_payoff(legs, D(85)) == D(-3)  # below put wing: max loss
    assert total_payoff(legs, D(115)) == D(-3)  # above call wing: max loss


def test_iron_condor_rejects_wrong_strike_order():
    with pytest.raises(ValueError):
        strategies.iron_condor(
            put_long_strike=D(95),  # should be < put_short_strike
            put_long_premium=D(1),
            put_short_strike=D(90),
            put_short_premium=D(2),
            call_short_strike=D(105),
            call_short_premium=D(2),
            call_long_strike=D(110),
            call_long_premium=D(1),
        )


def test_long_call_butterfly():
    legs = strategies.long_call_butterfly(
        lower_strike=D(90),
        lower_premium=D(12),
        middle_strike=D(100),
        middle_premium=D(6),
        upper_strike=D(110),
        upper_premium=D(2),
    )
    result = analyze(legs)

    assert result.net_premium == D(2)  # debit: 12 - 2*6 + 2
    assert result.max_profit == D(8)  # wing width (10) - net debit (2)
    assert result.max_loss == D(2)
    assert result.breakevens == (D(92), D(108))
    assert total_payoff(legs, D(100)) == D(8)  # pins exactly at the short middle strike
    assert total_payoff(legs, D(90)) == D(-2)
    assert total_payoff(legs, D(110)) == D(-2)


def test_long_call_butterfly_rejects_wrong_strike_order():
    with pytest.raises(ValueError):
        strategies.long_call_butterfly(
            lower_strike=D(100),  # should be < middle_strike
            lower_premium=D(6),
            middle_strike=D(90),
            middle_premium=D(12),
            upper_strike=D(110),
            upper_premium=D(2),
        )


def test_iron_butterfly():
    legs = strategies.iron_butterfly(
        put_long_strike=D(90),
        put_long_premium=D(1),
        center_strike=D(100),
        put_short_premium=D(5),
        call_short_premium=D(5),
        call_long_strike=D(110),
        call_long_premium=D(1),
    )
    result = analyze(legs)

    assert result.net_premium == D(-8)  # net credit: -5 -5 +1 +1
    assert result.max_profit == D(8)  # credit received, at the center strike
    assert result.max_loss == D(2)  # wing width (10) - credit (8)
    assert result.breakevens == (D(92), D(108))
    assert total_payoff(legs, D(100)) == D(8)
    assert total_payoff(legs, D(90)) == D(-2)
    assert total_payoff(legs, D(110)) == D(-2)


def test_iron_butterfly_rejects_wrong_strike_order():
    with pytest.raises(ValueError):
        strategies.iron_butterfly(
            put_long_strike=D(100),  # should be < center_strike
            put_long_premium=D(1),
            center_strike=D(90),
            put_short_premium=D(5),
            call_short_premium=D(5),
            call_long_strike=D(110),
            call_long_premium=D(1),
        )


def test_analyze_rejects_empty_legs():
    with pytest.raises(ValueError):
        analyze([])
