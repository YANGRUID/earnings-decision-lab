"""Deterministic strategy-candidate generation from a real options chain
(Phase 14). Given the real chain a provider has already returned for a
company's upcoming earnings, mechanically builds one candidate per
strategy category in the required set (see analytics/options/strategies.py)
using real strikes and real mid-market premiums -- no LLM involvement, no
interpolated strikes, no fabricated prices. A category is simply omitted
(never guessed) when the real chain doesn't have the strikes or quotes it
needs.

Strike selection is entirely index-based over the real, sorted, deduped
strikes present in the chain for a single expiration: "ATM" is the strike
nearest the underlying price; "N strikes out" is N positions further from
ATM in that sorted list. This matches how a real, often narrow options
chain (e.g. IBKR's bounded ~5-strikes-each-side window, see
providers/ibkr_options.py) actually looks, rather than assuming a fixed
percentage-OTM strike exists in the chain at all.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from analytics.options import strategies as strat
from analytics.options.implied_move import NoQuoteAvailable, mid_price
from analytics.options.payoff import OptionLeg, StrategyAnalysis, analyze
from providers.types import OptionQuote


class StrategyCategory(StrEnum):
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"
    PUT_CREDIT_SPREAD = "put_credit_spread"
    CALL_CREDIT_SPREAD = "call_credit_spread"
    LONG_STRADDLE = "long_straddle"
    LONG_STRANGLE = "long_strangle"
    IRON_CONDOR = "iron_condor"
    LONG_CALL_BUTTERFLY = "long_call_butterfly"
    IRON_BUTTERFLY = "iron_butterfly"


@dataclass(frozen=True)
class StrategyCandidate:
    category: StrategyCategory
    legs: tuple[OptionLeg, ...]
    analysis: StrategyAnalysis
    expiration: date
    underlying_price: Decimal


_Leg = tuple[Decimal, Decimal]  # (strike, mid premium)


def _strikes(quotes: list[OptionQuote], option_type: str) -> list[Decimal]:
    return sorted({q.strike for q in quotes if q.option_type == option_type})


def _atm_index(strikes: list[Decimal], underlying_price: Decimal) -> int | None:
    if not strikes:
        return None
    return min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying_price))


def _quote(quotes: list[OptionQuote], option_type: str, strike: Decimal) -> OptionQuote | None:
    return next((q for q in quotes if q.option_type == option_type and q.strike == strike), None)


def _leg_at_offset(
    quotes: list[OptionQuote], option_type: str, strikes: list[Decimal], atm_index: int, offset: int
) -> _Leg | None:
    """The (strike, mid premium) ``offset`` positions from ``atm_index`` in
    ``strikes``, or None if that position is out of range, unquoted, or has
    no priceable bid/ask/last -- every real reason this strategy leg can't
    be honestly built."""
    i = atm_index + offset
    if not (0 <= i < len(strikes)):
        return None
    strike = strikes[i]
    quote = _quote(quotes, option_type, strike)
    if quote is None:
        return None
    try:
        return strike, mid_price(quote)
    except NoQuoteAvailable:
        return None


def _build_long_call(
    quotes: list[OptionQuote], call_strikes: list[Decimal], idx: int
) -> list[OptionLeg] | None:
    leg = _leg_at_offset(quotes, "call", call_strikes, idx, 0)
    return strat.long_call(*leg) if leg else None


def _build_long_put(
    quotes: list[OptionQuote], put_strikes: list[Decimal], idx: int
) -> list[OptionLeg] | None:
    leg = _leg_at_offset(quotes, "put", put_strikes, idx, 0)
    return strat.long_put(*leg) if leg else None


def _build_bull_call_spread(
    quotes: list[OptionQuote], call_strikes: list[Decimal], idx: int
) -> list[OptionLeg] | None:
    long_leg = _leg_at_offset(quotes, "call", call_strikes, idx, 0)
    short_leg = _leg_at_offset(quotes, "call", call_strikes, idx, 1)
    if long_leg is None or short_leg is None:
        return None
    (ls, lp), (ss, sp) = long_leg, short_leg
    return strat.bull_call_spread(ls, lp, ss, sp) if ls < ss else None


def _build_bear_put_spread(
    quotes: list[OptionQuote], put_strikes: list[Decimal], idx: int
) -> list[OptionLeg] | None:
    long_leg = _leg_at_offset(quotes, "put", put_strikes, idx, 0)
    short_leg = _leg_at_offset(quotes, "put", put_strikes, idx, -1)
    if long_leg is None or short_leg is None:
        return None
    (ls, lp), (ss, sp) = long_leg, short_leg
    return strat.bear_put_spread(ls, lp, ss, sp) if ss < ls else None


def _build_put_credit_spread(
    quotes: list[OptionQuote], put_strikes: list[Decimal], idx: int
) -> list[OptionLeg] | None:
    # Sell one strike below ATM, buy two strikes below for protection.
    short_leg = _leg_at_offset(quotes, "put", put_strikes, idx, -1)
    long_leg = _leg_at_offset(quotes, "put", put_strikes, idx, -2)
    if short_leg is None or long_leg is None:
        return None
    (ss, sp), (ls, lp) = short_leg, long_leg
    return strat.put_credit_spread(ss, sp, ls, lp) if ls < ss else None


def _build_call_credit_spread(
    quotes: list[OptionQuote], call_strikes: list[Decimal], idx: int
) -> list[OptionLeg] | None:
    # Sell one strike above ATM, buy two strikes above for protection.
    short_leg = _leg_at_offset(quotes, "call", call_strikes, idx, 1)
    long_leg = _leg_at_offset(quotes, "call", call_strikes, idx, 2)
    if short_leg is None or long_leg is None:
        return None
    (ss, sp), (ls, lp) = short_leg, long_leg
    return strat.call_credit_spread(ss, sp, ls, lp) if ss < ls else None


def _build_long_straddle(
    quotes: list[OptionQuote],
    call_strikes: list[Decimal],
    put_strikes: list[Decimal],
    call_idx: int,
    put_idx: int,
) -> list[OptionLeg] | None:
    call_leg = _leg_at_offset(quotes, "call", call_strikes, call_idx, 0)
    put_leg = _leg_at_offset(quotes, "put", put_strikes, put_idx, 0)
    if call_leg is None or put_leg is None:
        return None
    (cs, cp), (ps, pp) = call_leg, put_leg
    # A real straddle needs one strike quoted on both sides -- if the
    # nearest-ATM call and put strikes differ, this chain can't honestly
    # support one at this expiration.
    return strat.long_straddle(cs, cp, pp) if cs == ps else None


def _build_long_strangle(
    quotes: list[OptionQuote],
    call_strikes: list[Decimal],
    put_strikes: list[Decimal],
    call_idx: int,
    put_idx: int,
) -> list[OptionLeg] | None:
    put_leg = _leg_at_offset(quotes, "put", put_strikes, put_idx, -1)
    call_leg = _leg_at_offset(quotes, "call", call_strikes, call_idx, 1)
    if put_leg is None or call_leg is None:
        return None
    (ps, pp), (cs, cp) = put_leg, call_leg
    return strat.long_strangle(ps, pp, cs, cp) if ps < cs else None


def _build_iron_condor(
    quotes: list[OptionQuote],
    call_strikes: list[Decimal],
    put_strikes: list[Decimal],
    call_idx: int,
    put_idx: int,
) -> list[OptionLeg] | None:
    put_short = _leg_at_offset(quotes, "put", put_strikes, put_idx, -1)
    put_long = _leg_at_offset(quotes, "put", put_strikes, put_idx, -2)
    call_short = _leg_at_offset(quotes, "call", call_strikes, call_idx, 1)
    call_long = _leg_at_offset(quotes, "call", call_strikes, call_idx, 2)
    if put_short is None or put_long is None or call_short is None or call_long is None:
        return None
    (pss, psp), (pls, plp) = put_short, put_long
    (css, csp), (cls, clp) = call_short, call_long
    if not (pls < pss < css < cls):
        return None
    return strat.iron_condor(pls, plp, pss, psp, css, csp, cls, clp)


def _build_long_call_butterfly(
    quotes: list[OptionQuote], call_strikes: list[Decimal], idx: int
) -> list[OptionLeg] | None:
    lower = _leg_at_offset(quotes, "call", call_strikes, idx, -1)
    middle = _leg_at_offset(quotes, "call", call_strikes, idx, 0)
    upper = _leg_at_offset(quotes, "call", call_strikes, idx, 1)
    if lower is None or middle is None or upper is None:
        return None
    (lo_s, lo_p), (mid_s, mid_p), (up_s, up_p) = lower, middle, upper
    if not (lo_s < mid_s < up_s):
        return None
    return strat.long_call_butterfly(lo_s, lo_p, mid_s, mid_p, up_s, up_p)


def _build_iron_butterfly(
    quotes: list[OptionQuote],
    call_strikes: list[Decimal],
    put_strikes: list[Decimal],
    call_idx: int,
    put_idx: int,
) -> list[OptionLeg] | None:
    put_short = _leg_at_offset(quotes, "put", put_strikes, put_idx, 0)
    call_short = _leg_at_offset(quotes, "call", call_strikes, call_idx, 0)
    put_long = _leg_at_offset(quotes, "put", put_strikes, put_idx, -1)
    call_long = _leg_at_offset(quotes, "call", call_strikes, call_idx, 1)
    if put_short is None or call_short is None or put_long is None or call_long is None:
        return None
    (pss, psp), (css, csp) = put_short, call_short
    (pls, plp), (cls, clp) = put_long, call_long
    # A real iron butterfly needs one shared center strike -- if the
    # nearest-ATM put and call strikes differ, this chain can't honestly
    # support one at this expiration (same rule the long straddle uses).
    if pss != css:
        return None
    center = pss
    if not (pls < center < cls):
        return None
    return strat.iron_butterfly(pls, plp, center, psp, csp, cls, clp)


def generate_candidates(
    quotes: list[OptionQuote], underlying_price: Decimal, expiration: date
) -> list[StrategyCandidate]:
    """Builds every strategy category the real chain supports, for a single
    expiration. ``quotes`` may span multiple expirations -- only the ones
    matching ``expiration`` are used (mirrors
    calculate_atm_straddle_implied_move's contract). Categories whose real
    strikes/quotes aren't available are omitted, never fabricated -- the
    result can legitimately be a partial list, or empty.
    """
    same_expiration = [q for q in quotes if q.expiration_date == expiration]
    call_strikes = _strikes(same_expiration, "call")
    put_strikes = _strikes(same_expiration, "put")
    call_idx = _atm_index(call_strikes, underlying_price)
    put_idx = _atm_index(put_strikes, underlying_price)

    builders: list[tuple[StrategyCategory, list[OptionLeg] | None]] = []
    if call_idx is not None:
        builders.append(
            (StrategyCategory.LONG_CALL, _build_long_call(same_expiration, call_strikes, call_idx))
        )
        builders.append(
            (
                StrategyCategory.BULL_CALL_SPREAD,
                _build_bull_call_spread(same_expiration, call_strikes, call_idx),
            )
        )
        builders.append(
            (
                StrategyCategory.CALL_CREDIT_SPREAD,
                _build_call_credit_spread(same_expiration, call_strikes, call_idx),
            )
        )
        builders.append(
            (
                StrategyCategory.LONG_CALL_BUTTERFLY,
                _build_long_call_butterfly(same_expiration, call_strikes, call_idx),
            )
        )
    if put_idx is not None:
        builders.append(
            (StrategyCategory.LONG_PUT, _build_long_put(same_expiration, put_strikes, put_idx))
        )
        builders.append(
            (
                StrategyCategory.BEAR_PUT_SPREAD,
                _build_bear_put_spread(same_expiration, put_strikes, put_idx),
            )
        )
        builders.append(
            (
                StrategyCategory.PUT_CREDIT_SPREAD,
                _build_put_credit_spread(same_expiration, put_strikes, put_idx),
            )
        )
    if call_idx is not None and put_idx is not None:
        builders.append(
            (
                StrategyCategory.LONG_STRADDLE,
                _build_long_straddle(same_expiration, call_strikes, put_strikes, call_idx, put_idx),
            )
        )
        builders.append(
            (
                StrategyCategory.LONG_STRANGLE,
                _build_long_strangle(same_expiration, call_strikes, put_strikes, call_idx, put_idx),
            )
        )
        builders.append(
            (
                StrategyCategory.IRON_CONDOR,
                _build_iron_condor(same_expiration, call_strikes, put_strikes, call_idx, put_idx),
            )
        )
        builders.append(
            (
                StrategyCategory.IRON_BUTTERFLY,
                _build_iron_butterfly(
                    same_expiration, call_strikes, put_strikes, call_idx, put_idx
                ),
            )
        )

    candidates = []
    for category, legs in builders:
        if legs is None:
            continue
        candidates.append(
            StrategyCandidate(
                category=category,
                legs=tuple(legs),
                analysis=analyze(legs),
                expiration=expiration,
                underlying_price=underlying_price,
            )
        )
    return candidates
