"""Options sentiment metrics computed from real ingested options-chain
quotes: put/call ratios (open interest and volume) and ATM IV term
structure between two specific expirations. Every value here is plain
arithmetic on stored quotes -- never a directional "bullish"/"bearish"
label. A put/call ratio or IV term structure slope doesn't, by itself,
support a sentiment claim without more context than this project has; see
docs/options_methodology.md.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from analytics.options.implied_move import AtmIvResult, calculate_atm_iv, find_atm_strike
from providers.types import OptionQuote


def _ratio(call_total: int, put_total: int) -> Decimal | None:
    """None when calls have zero total (an undefined ratio), not zero -- a
    0 ratio would misleadingly imply "no put interest" rather than "no
    basis to compute one".
    """
    if call_total == 0:
        return None
    return Decimal(put_total) / Decimal(call_total)


def put_call_open_interest_ratio(quotes: list[OptionQuote]) -> Decimal | None:
    call_total = sum((q.open_interest or 0) for q in quotes if q.option_type == "call")
    put_total = sum((q.open_interest or 0) for q in quotes if q.option_type == "put")
    return _ratio(call_total, put_total)


def put_call_volume_ratio(quotes: list[OptionQuote]) -> Decimal | None:
    call_total = sum((q.volume or 0) for q in quotes if q.option_type == "call")
    put_total = sum((q.volume or 0) for q in quotes if q.option_type == "put")
    return _ratio(call_total, put_total)


def atm_iv_at_expiration(
    quotes: list[OptionQuote], expiration: date, underlying_price: Decimal
) -> AtmIvResult | None:
    """None when there's no call+put pair at the ATM strike for this
    expiration to compute IV from."""
    same_expiration = [q for q in quotes if q.expiration_date == expiration]
    if not same_expiration:
        return None
    atm_strike = find_atm_strike(same_expiration, underlying_price)
    call = next(
        (q for q in same_expiration if q.strike == atm_strike and q.option_type == "call"), None
    )
    put = next(
        (q for q in same_expiration if q.strike == atm_strike and q.option_type == "put"), None
    )
    if call is None or put is None:
        return None
    return calculate_atm_iv(call, put)


@dataclass(frozen=True)
class IvTermStructure:
    near_expiration: date
    near_atm_iv: Decimal | None
    next_expiration: date
    next_atm_iv: Decimal | None
    slope: Decimal | None


def iv_term_structure(
    quotes: list[OptionQuote],
    near_expiration: date,
    next_expiration: date,
    underlying_price: Decimal,
) -> IvTermStructure:
    """Compares ATM IV between two specific expirations -- the caller
    chooses which two (typically the near-term expiration after the
    earnings date and the next available expiration after that, e.g. via
    two calls to select_expiration_after). ``slope`` is
    next_atm_iv - near_atm_iv: positive means the market prices more
    uncertainty further out. None when either side's ATM IV isn't
    available, never a fabricated value.
    """
    near = atm_iv_at_expiration(quotes, near_expiration, underlying_price)
    next_point = atm_iv_at_expiration(quotes, next_expiration, underlying_price)
    near_iv = near.atm_iv if near else None
    next_iv = next_point.atm_iv if next_point else None
    slope = (next_iv - near_iv) if (near_iv is not None and next_iv is not None) else None

    return IvTermStructure(
        near_expiration=near_expiration,
        near_atm_iv=near_iv,
        next_expiration=next_expiration,
        next_atm_iv=next_iv,
        slope=slope,
    )
