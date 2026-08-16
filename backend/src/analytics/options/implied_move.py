"""Earnings implied-move calculation.

Implements exactly one documented methodology: the near-ATM straddle
approximation (implied move ≈ (ATM call mid + ATM put mid) / underlying
price). This is a standard, widely-used approximation, **not presented as
the only correct one** — see docs/options_methodology.md for alternatives
(e.g. a wider strangle-based estimate, or a variance-swap-style
calculation) and why this one was implemented first. Every result carries
its method name, inputs, expiration, and timestamp so it's auditable and
never silently conflated with a different methodology's output — this is
exactly what ``VolatilitySnapshot.inputs`` (models/volatility_snapshot.py)
exists to store.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from providers.types import OptionQuote

METHOD_ATM_STRADDLE = "atm_straddle"


class NoQuoteAvailable(Exception):
    pass


@dataclass(frozen=True)
class ImpliedMoveResult:
    method: str
    expiration: date
    atm_strike: Decimal
    call_mid: Decimal
    put_mid: Decimal
    underlying_price: Decimal
    implied_move_pct: Decimal
    implied_move_absolute: Decimal
    computed_at: datetime

    def as_inputs_json(self) -> dict:
        """Shape matching VolatilitySnapshot.inputs — the audit trail."""
        return {
            "method": self.method,
            "expiration": self.expiration.isoformat(),
            "atm_strike": str(self.atm_strike),
            "call_mid": str(self.call_mid),
            "put_mid": str(self.put_mid),
            "underlying_price": str(self.underlying_price),
        }


def mid_price(quote: OptionQuote) -> Decimal:
    if quote.bid is not None and quote.ask is not None:
        return (quote.bid + quote.ask) / 2
    if quote.last_price is not None:
        return quote.last_price
    raise NoQuoteAvailable(f"no bid/ask or last_price on quote for strike {quote.strike}")


def find_atm_strike(quotes: list[OptionQuote], underlying_price: Decimal) -> Decimal:
    strikes = {q.strike for q in quotes}
    if not strikes:
        raise NoQuoteAvailable("no quotes provided")
    return min(strikes, key=lambda k: abs(k - underlying_price))


def calculate_atm_straddle_implied_move(
    quotes: list[OptionQuote],
    expiration: date,
    underlying_price: Decimal,
    computed_at: datetime,
) -> ImpliedMoveResult:
    """``quotes`` should be every call+put quote for a single expiration.
    Selects the strike nearest ``underlying_price`` and prices a straddle
    at that strike.
    """
    same_expiration = [q for q in quotes if q.expiration_date == expiration]
    if not same_expiration:
        raise NoQuoteAvailable(f"no quotes for expiration {expiration}")

    atm_strike = find_atm_strike(same_expiration, underlying_price)
    call = next(
        (q for q in same_expiration if q.strike == atm_strike and q.option_type == "call"), None
    )
    put = next(
        (q for q in same_expiration if q.strike == atm_strike and q.option_type == "put"), None
    )
    if call is None or put is None:
        raise NoQuoteAvailable(f"missing call or put at ATM strike {atm_strike}")

    call_mid = mid_price(call)
    put_mid = mid_price(put)
    straddle_price = call_mid + put_mid
    implied_move_pct = straddle_price / underlying_price

    return ImpliedMoveResult(
        method=METHOD_ATM_STRADDLE,
        expiration=expiration,
        atm_strike=atm_strike,
        call_mid=call_mid,
        put_mid=put_mid,
        underlying_price=underlying_price,
        implied_move_pct=implied_move_pct,
        implied_move_absolute=straddle_price,
        computed_at=computed_at,
    )
