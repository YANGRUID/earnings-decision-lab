"""V4 end-of-day settlement fallback -- the explicit, permanent answer to
"the required executable side does not exist at the settlement instant".

Authorized by the product owner on 2026-09-04, after the required-side
incident proved this is not a transient data problem: a deep-OTM leg whose
bid book is genuinely empty (IBKR: a -1 price tick with a 0 size) can never
produce a BID, so under the executable-only rule those positions would stay
open forever through no fault of the market data.

The hierarchy, in strict order, per leg:

  1. EXECUTABLE_BID / EXECUTABLE_ASK -- the normal rule, unchanged. A real
     required-side quote captured at or before the close: close a long at
     the BID, a short at the ASK. Nothing below is consulted while this
     exists.
  2. MARKET_CLOSE_FALLBACK -- that option contract's own final verifiable
     same-session closing mark from IBKR for the settlement date. A closing
     mark, explicitly NOT an executable trade, and labelled as such.
  3. EXPIRATION_INTRINSIC_AT_CLOSE -- only for a contract expiring ON the
     settlement date, and only when no usable option closing mark exists:
     intrinsic value against the official underlying close
     (call = max(U - K, 0), put = max(K - U, 0)).

A contract that is NOT expiring on the settlement date never reaches step 3:
it is not written down to zero merely because its book was empty, because a
live option with time value left is not worth zero -- it is simply unquoted,
and stays unresolved until a real closing mark exists.

Deliberately absent, and never to be added here without the product owner
saying so: Black-Scholes or any other model price, a prior-day/historical
price, a midpoint, a last-trade substitution for the required side, or any
synthesised number. Every resolution carries the provenance of what was
actually used.
"""

from dataclasses import dataclass, field
from decimal import Decimal

# Explicit pricing-source labels persisted with every settled leg.
PRICING_EXECUTABLE_BID = "EXECUTABLE_BID"
PRICING_EXECUTABLE_ASK = "EXECUTABLE_ASK"
PRICING_MARKET_CLOSE_FALLBACK = "MARKET_CLOSE_FALLBACK"
PRICING_EXPIRATION_INTRINSIC_AT_CLOSE = "EXPIRATION_INTRINSIC_AT_CLOSE"

EXECUTABLE_PRICING_SOURCES = frozenset(
    {PRICING_EXECUTABLE_BID, PRICING_EXECUTABLE_ASK}
)
FALLBACK_PRICING_SOURCES = frozenset(
    {PRICING_MARKET_CLOSE_FALLBACK, PRICING_EXPIRATION_INTRINSIC_AT_CLOSE}
)

# Why a leg could not be priced at all, when even the fallbacks are empty.
UNRESOLVED_NO_CLOSING_MARK = "NO_CLOSING_MARK"
UNRESOLVED_NO_UNDERLYING_CLOSE = "NO_UNDERLYING_CLOSE"


@dataclass(frozen=True)
class LegExitPrice:
    """One leg's resolved exit price and exactly where it came from."""

    price: Decimal | None
    pricing_source: str | None
    required_side: str
    unresolved_reason: str | None = None
    provenance: dict = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.price is not None

    @property
    def is_executable(self) -> bool:
        return self.pricing_source in EXECUTABLE_PRICING_SOURCES


def required_exit_side(action: str) -> str:
    """Unchanged V4 convention: a long leg is closed at the BID, a short leg
    at the ASK."""
    return "bid" if action == "buy" else "ask"


def expiration_intrinsic(right: str, strike: Decimal, underlying_close: Decimal) -> Decimal:
    """Settlement intrinsic value at expiration against the official
    underlying close. Never used for a contract that is still alive."""
    if right == "call":
        return max(underlying_close - strike, Decimal(0))
    return max(strike - underlying_close, Decimal(0))


def resolve_leg_exit_price(
    *,
    action: str,
    right: str,
    strike: Decimal,
    executable_price: Decimal | None,
    session_close: Decimal | None,
    underlying_close: Decimal | None,
    expires_on_settlement_date: bool,
    book_empty: bool | None = None,
    market_data_quality: str | None = None,
) -> LegExitPrice:
    """Apply the hierarchy above to ONE leg. Pure -- every input is already
    an observed fact, and nothing here reaches a provider."""
    side = required_exit_side(action)
    provenance: dict = {
        "required_side": side,
        "book_empty": book_empty,
        "market_data_quality": market_data_quality,
        "expires_on_settlement_date": expires_on_settlement_date,
    }

    if executable_price is not None:
        return LegExitPrice(
            price=executable_price,
            pricing_source=(
                PRICING_EXECUTABLE_BID if side == "bid" else PRICING_EXECUTABLE_ASK
            ),
            required_side=side,
            provenance=provenance,
        )

    if session_close is not None:
        return LegExitPrice(
            price=session_close,
            pricing_source=PRICING_MARKET_CLOSE_FALLBACK,
            required_side=side,
            provenance={**provenance, "option_session_close": str(session_close)},
        )

    if not expires_on_settlement_date:
        # Rule 4: a living option is not worth zero just because nobody was
        # bidding for it at the settlement instant.
        return LegExitPrice(
            price=None,
            pricing_source=None,
            required_side=side,
            unresolved_reason=UNRESOLVED_NO_CLOSING_MARK,
            provenance=provenance,
        )

    if underlying_close is None:
        return LegExitPrice(
            price=None,
            pricing_source=None,
            required_side=side,
            unresolved_reason=UNRESOLVED_NO_UNDERLYING_CLOSE,
            provenance=provenance,
        )

    intrinsic = expiration_intrinsic(right, strike, underlying_close)
    return LegExitPrice(
        price=intrinsic,
        pricing_source=PRICING_EXPIRATION_INTRINSIC_AT_CLOSE,
        required_side=side,
        provenance={
            **provenance,
            "underlying_close": str(underlying_close),
            "strike": str(strike),
            "right": right,
        },
    )
