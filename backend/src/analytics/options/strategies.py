"""Named constructors for the required strategy set. Each returns a list of
``OptionLeg``s — pass the result to ``payoff.analyze()`` for max
profit/loss/breakeven, or to ``payoff.total_payoff()`` for a specific
underlying-price scenario. Strike ordering is validated so a mixed-up call
site fails loudly instead of silently producing a nonsensical strategy.
"""

from decimal import Decimal

from analytics.options.payoff import Action, OptionLeg
from models.enums import OptionType


def long_call(strike: Decimal, premium: Decimal) -> list[OptionLeg]:
    return [OptionLeg(OptionType.CALL, Action.BUY, strike, premium)]


def long_put(strike: Decimal, premium: Decimal) -> list[OptionLeg]:
    return [OptionLeg(OptionType.PUT, Action.BUY, strike, premium)]


def bull_call_spread(
    long_strike: Decimal, long_premium: Decimal, short_strike: Decimal, short_premium: Decimal
) -> list[OptionLeg]:
    if not long_strike < short_strike:
        raise ValueError("bull call spread requires long_strike < short_strike")
    return [
        OptionLeg(OptionType.CALL, Action.BUY, long_strike, long_premium),
        OptionLeg(OptionType.CALL, Action.SELL, short_strike, short_premium),
    ]


def bear_put_spread(
    long_strike: Decimal, long_premium: Decimal, short_strike: Decimal, short_premium: Decimal
) -> list[OptionLeg]:
    if not short_strike < long_strike:
        raise ValueError("bear put spread requires short_strike < long_strike")
    return [
        OptionLeg(OptionType.PUT, Action.BUY, long_strike, long_premium),
        OptionLeg(OptionType.PUT, Action.SELL, short_strike, short_premium),
    ]


def put_credit_spread(
    short_strike: Decimal, short_premium: Decimal, long_strike: Decimal, long_premium: Decimal
) -> list[OptionLeg]:
    """Bull put spread: sell the higher-strike put, buy the lower-strike put
    for protection. Net premium should come out negative (a credit)."""
    if not long_strike < short_strike:
        raise ValueError("put credit spread requires long_strike < short_strike")
    return [
        OptionLeg(OptionType.PUT, Action.SELL, short_strike, short_premium),
        OptionLeg(OptionType.PUT, Action.BUY, long_strike, long_premium),
    ]


def call_credit_spread(
    short_strike: Decimal, short_premium: Decimal, long_strike: Decimal, long_premium: Decimal
) -> list[OptionLeg]:
    """Bear call spread: sell the lower-strike call, buy the higher-strike
    call for protection. Net premium should come out negative (a credit)."""
    if not short_strike < long_strike:
        raise ValueError("call credit spread requires short_strike < long_strike")
    return [
        OptionLeg(OptionType.CALL, Action.SELL, short_strike, short_premium),
        OptionLeg(OptionType.CALL, Action.BUY, long_strike, long_premium),
    ]


def long_straddle(strike: Decimal, call_premium: Decimal, put_premium: Decimal) -> list[OptionLeg]:
    return [
        OptionLeg(OptionType.CALL, Action.BUY, strike, call_premium),
        OptionLeg(OptionType.PUT, Action.BUY, strike, put_premium),
    ]


def long_strangle(
    put_strike: Decimal, put_premium: Decimal, call_strike: Decimal, call_premium: Decimal
) -> list[OptionLeg]:
    if not put_strike < call_strike:
        raise ValueError("long strangle requires put_strike < call_strike")
    return [
        OptionLeg(OptionType.PUT, Action.BUY, put_strike, put_premium),
        OptionLeg(OptionType.CALL, Action.BUY, call_strike, call_premium),
    ]


def iron_condor(
    put_long_strike: Decimal,
    put_long_premium: Decimal,
    put_short_strike: Decimal,
    put_short_premium: Decimal,
    call_short_strike: Decimal,
    call_short_premium: Decimal,
    call_long_strike: Decimal,
    call_long_premium: Decimal,
) -> list[OptionLeg]:
    """Short iron condor: sell the inner put/call, buy the outer put/call
    wings for protection. Strikes must satisfy
    put_long < put_short < call_short < call_long."""
    if not (put_long_strike < put_short_strike < call_short_strike < call_long_strike):
        raise ValueError(
            "iron condor requires put_long_strike < put_short_strike "
            "< call_short_strike < call_long_strike"
        )
    return [
        OptionLeg(OptionType.PUT, Action.BUY, put_long_strike, put_long_premium),
        OptionLeg(OptionType.PUT, Action.SELL, put_short_strike, put_short_premium),
        OptionLeg(OptionType.CALL, Action.SELL, call_short_strike, call_short_premium),
        OptionLeg(OptionType.CALL, Action.BUY, call_long_strike, call_long_premium),
    ]
