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

from models.enums import OptionsSnapshotAnchor
from providers.types import OptionQuote

METHOD_ATM_STRADDLE = "atm_straddle"

# ATM IV methodology labels -- surfaced to the UI so "how was this number
# computed" is never ambiguous when call and put IV disagree (a real,
# expected occurrence, not an error condition -- see calculate_atm_iv).
ATM_IV_METHOD_AVERAGE = "average_of_call_and_put_iv"
ATM_IV_METHOD_SINGLE_SIDE = "single_side_only"
# Absolute IV difference (e.g. 0.05 = 5 vol points) above which call/put IV
# at the same strike are flagged as diverging rather than just averaged
# silently. A real, deliberately simple threshold, not a statistically
# derived one -- see docs/options_methodology.md.
IV_DIVERGENCE_THRESHOLD = Decimal("0.05")


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


def select_expiration_after(available_expirations: set[date], earnings_date: date) -> date | None:
    """Nearest available expiration strictly *after* ``earnings_date`` —
    the documented rule for picking which expiration's pricing actually
    reflects the earnings move. An expiration on or before the earnings
    date wouldn't have the event inside its remaining life at all, so it's
    never a candidate regardless of how close it is. Returns ``None`` if
    every available expiration is on or before the earnings date (no
    forward-looking expiration exists in the chain).
    """
    candidates = [exp for exp in available_expirations if exp > earnings_date]
    return min(candidates) if candidates else None


def select_nearest_listed_expiration(
    available_expirations: set[date], reference_date: date
) -> date | None:
    """Nearest available expiration *on or after* ``reference_date`` — the
    documented fallback rule for a general/current (non-earnings-anchored)
    options snapshot, where there is no earnings date to require
    strictly-after semantics for (see ``select_expiration_after``, which
    this deliberately does NOT reuse: a same-day expiration is a real,
    usable "current" data point when there's no earnings event it needs to
    outlive). Returns ``None`` if every available expiration is before
    ``reference_date``.
    """
    candidates = [exp for exp in available_expirations if exp >= reference_date]
    return min(candidates) if candidates else None


def select_target_expiration_and_anchor(
    available_expirations: set[date],
    earnings_date: date | None,
    reference_date: date,
) -> tuple[date | None, OptionsSnapshotAnchor]:
    """Single, shared rule for picking which expiration in an
    already-collected chain represents "the" snapshot expiration, and
    whether that selection is earnings-anchored or general/current — used
    identically everywhere a persisted chain needs both an expiration and
    an anchor label derived from it (compute_and_persist_volatility_snapshot,
    generate_strategy_candidates), so two call sites can never disagree
    about what a given chain represents.
    """
    if earnings_date is not None:
        return select_expiration_after(available_expirations, earnings_date), (
            OptionsSnapshotAnchor.EARNINGS_ANCHORED
        )
    return select_nearest_listed_expiration(available_expirations, reference_date), (
        OptionsSnapshotAnchor.GENERAL_CURRENT
    )


@dataclass(frozen=True)
class AtmIvResult:
    method: str
    call_iv: Decimal | None
    put_iv: Decimal | None
    atm_iv: Decimal | None
    diverges: bool


def calculate_atm_iv(
    call: OptionQuote,
    put: OptionQuote,
    divergence_threshold: Decimal = IV_DIVERGENCE_THRESHOLD,
) -> AtmIvResult:
    """ATM IV from the same call/put pair ``calculate_atm_straddle_implied_move``
    uses. When both sides report IV, the methodology is a plain average —
    the simplest defensible choice, not presented as the only one (see
    docs/options_methodology.md) — and ``diverges`` flags when call and put
    IV disagree by more than ``divergence_threshold``, which the UI must
    surface rather than silently average away. When only one side reports
    IV (a real, observed possibility, not a bug), that side's value is used
    directly and the method is labeled accordingly so it's never confused
    with a genuine two-sided average.
    """
    call_iv, put_iv = call.implied_volatility, put.implied_volatility
    if call_iv is not None and put_iv is not None:
        return AtmIvResult(
            method=ATM_IV_METHOD_AVERAGE,
            call_iv=call_iv,
            put_iv=put_iv,
            atm_iv=(call_iv + put_iv) / 2,
            diverges=abs(call_iv - put_iv) > divergence_threshold,
        )
    single = call_iv if call_iv is not None else put_iv
    return AtmIvResult(
        method=ATM_IV_METHOD_SINGLE_SIDE,
        call_iv=call_iv,
        put_iv=put_iv,
        atm_iv=single,
        diverges=False,
    )
