"""Options Decision Engine V4.3 -- Target-Price -> Listed-Strike
Resolver (2026-09-02).

ONE deterministic function every V4.3 strategy-construction routine
uses to turn an economic TARGET price into a real, listed strike
actually present in the real chain -- never synthesizing a strike that
doesn't exist (this task's own Section 5). V3's own strike selection
(analytics/options/strategy_candidates.py) is untouched and does not
use this module; it remains pure index-offset-from-ATM, as confirmed
by this task's own Section 1 audit.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from providers.types import OptionQuote

Right = Literal["call", "put"]
QuoteQuality = Literal["two_sided", "one_sided", "unquoted"]


class StrikeConstraint(StrEnum):
    NEAREST = "nearest"
    NEAREST_ABOVE = "nearest_above"
    NEAREST_BELOW = "nearest_below"


@dataclass(frozen=True)
class StrikeResolution:
    target_price: Decimal
    right: Right
    constraint: StrikeConstraint
    resolvable: bool
    selected_strike: Decimal | None
    distance_dollars: Decimal | None
    distance_pct: Decimal | None
    strike_index: int | None
    chain_size: int
    external_contract_id: str | None
    quote: OptionQuote | None
    quote_quality: QuoteQuality | None
    # True when the target lay beyond every real strike available on
    # this side, so resolution clamped to the chain's own edge --
    # a real, disclosed degradation (Section 27: "must gracefully map
    # targets to real listed strikes"), never a silent substitution.
    hit_chain_edge: bool
    reason: str | None


def _quote_quality(quote: OptionQuote | None) -> QuoteQuality:
    if quote is None:
        return "unquoted"
    if quote.bid is not None and quote.ask is not None:
        return "two_sided"
    if quote.last_price is not None:
        return "one_sided"
    return "unquoted"


def _spread_pct(quote: OptionQuote | None) -> Decimal:
    """Missing/degenerate bid-ask is treated as maximally bad for
    tie-breaking purposes -- never a crash on an absent quote."""
    if quote is None or quote.bid is None or quote.ask is None or quote.bid <= 0:
        return Decimal("Infinity")
    return (quote.ask - quote.bid) / quote.bid


_QUALITY_RANK: dict[QuoteQuality, int] = {"two_sided": 0, "one_sided": 1, "unquoted": 2}


def _tie_break_key(quote: OptionQuote | None, strike: Decimal) -> tuple[int, Decimal, int, Decimal]:
    """Deterministic tie-break order (this task's own Section 6),
    applied only when two candidate strikes are EXACTLY equidistant
    from the target: (1) better real quote coverage -- two-sided beats
    one-sided beats unquoted; (2) narrower bid/ask spread %; (3)
    stronger combined OI+volume; (4) final deterministic fallback, the
    lower strike, so the rule never depends on incidental set/dict
    iteration order. This is strike-resolution tie-breaking only --
    never the full V4 ranking engine (Section 6's own instruction)."""
    quality_rank = _QUALITY_RANK[_quote_quality(quote)]
    spread = _spread_pct(quote)
    liquidity = -((quote.open_interest or 0) + (quote.volume or 0)) if quote else 0
    return (quality_rank, spread, liquidity, strike)


def resolve_target_to_strike(
    target_price: Decimal,
    right: Right,
    quotes: list[OptionQuote],
    constraint: StrikeConstraint = StrikeConstraint.NEAREST,
) -> StrikeResolution:
    """Deterministic; never invents a strike -- only ever returns a
    strike genuinely present in ``quotes``. When ``constraint`` cannot
    be honored at all (e.g. NEAREST_ABOVE but every real strike lies
    below the target), returns resolvable=False with a real reason
    rather than silently reinterpreting the constraint as NEAREST."""
    side_quotes = [q for q in quotes if q.option_type == right]
    strikes = sorted({q.strike for q in side_quotes})
    chain_size = len(strikes)
    if not strikes:
        return StrikeResolution(
            target_price=target_price,
            right=right,
            constraint=constraint,
            resolvable=False,
            selected_strike=None,
            distance_dollars=None,
            distance_pct=None,
            strike_index=None,
            chain_size=0,
            external_contract_id=None,
            quote=None,
            quote_quality=None,
            hit_chain_edge=False,
            reason=f"no listed {right} strikes available in the real chain",
        )

    if constraint == StrikeConstraint.NEAREST_ABOVE:
        candidates = [s for s in strikes if s >= target_price]
    elif constraint == StrikeConstraint.NEAREST_BELOW:
        candidates = [s for s in strikes if s <= target_price]
    else:
        candidates = strikes

    if not candidates:
        return StrikeResolution(
            target_price=target_price,
            right=right,
            constraint=constraint,
            resolvable=False,
            selected_strike=None,
            distance_dollars=None,
            distance_pct=None,
            strike_index=None,
            chain_size=chain_size,
            external_contract_id=None,
            quote=None,
            quote_quality=None,
            hit_chain_edge=False,
            reason=(
                f"no real {right} strike satisfies constraint {constraint.value} "
                f"relative to target {target_price}"
            ),
        )

    def _quote_for(strike: Decimal) -> OptionQuote | None:
        return next((q for q in side_quotes if q.strike == strike), None)

    min_distance = min(abs(s - target_price) for s in candidates)
    nearest = [s for s in candidates if abs(s - target_price) == min_distance]
    selected = min(nearest, key=lambda s: _tie_break_key(_quote_for(s), s))

    candidates_sorted = sorted(candidates)
    hit_chain_edge = (selected == candidates_sorted[0] and target_price < candidates_sorted[0]) or (
        selected == candidates_sorted[-1] and target_price > candidates_sorted[-1]
    )

    quote = _quote_for(selected)
    distance_dollars = selected - target_price
    distance_pct = distance_dollars / target_price if target_price != 0 else None

    return StrikeResolution(
        target_price=target_price,
        right=right,
        constraint=constraint,
        resolvable=True,
        selected_strike=selected,
        distance_dollars=distance_dollars,
        distance_pct=distance_pct,
        strike_index=strikes.index(selected),
        chain_size=chain_size,
        external_contract_id=quote.external_contract_id if quote else None,
        quote=quote,
        quote_quality=_quote_quality(quote),
        hit_chain_edge=hit_chain_edge,
        reason=None,
    )


def next_strike_beyond(
    quotes: list[OptionQuote],
    right: Right,
    reference_strike: Decimal,
    direction: Literal["up", "down"],
) -> Decimal | None:
    """The real next listed strike strictly beyond ``reference_strike``
    -- used for protective-wing placement (Sections 12/13): the
    tightest possible real protective wing available in the actual
    chain, i.e. the minimum viable width real risk-sizing (V4.4) will
    later refine. None if the chain has nothing further out."""
    strikes = sorted({q.strike for q in quotes if q.option_type == right})
    if direction == "up":
        beyond = [s for s in strikes if s > reference_strike]
        return min(beyond) if beyond else None
    beyond = [s for s in strikes if s < reference_strike]
    return max(beyond) if beyond else None
