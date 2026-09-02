"""Options Decision Engine V4.3 -- Expected-Move-Aware Strike Selection
(2026-09-02).

Answers ONE question, deterministically, from real options-payoff
economics: given a strategy family V4.2 already found semantically
compatible with the stated view, WHERE should its strikes actually be?
Never "which strategy family" (V4.2's job) and never "how good is this
trade overall / what should the final ranking be" (V4.4's job, not
built here).

V3's real strike selection (analytics/options/strategy_candidates.py)
is confirmed, by direct re-reading of its current source (this task's
own Section 1), to be purely index-offset-from-ATM: it uses only the
real chain's own sorted strike list and the underlying spot price to
find "N strikes out from ATM" -- no implied move, no historical move,
no delta, no IV, no liquidity signal ever enters WHICH strike is
picked. This module replaces none of that: V3 is completely untouched,
byte for byte, and this module is reachable only through tests, the
read-only replay module, and a clearly-experimental API route.

DIRECTION IS NOT A SEPARATE STRIKE-GEOMETRY INPUT. The requested
``StrategyCategory`` already encodes the directional intent a
separately-supplied view would otherwise duplicate (see
v4_strategy_semantics.py's own registry: LONG_CALL is already
"bullish", CALL_CREDIT_SPREAD already "bearish", etc.) -- which family
to build is V4.2's job, upstream of this module. Accepting a second,
parallel direction parameter here would create exactly the kind of risk
this whole V4 project exists to eliminate: two signals that could
silently disagree. Strike geometry is therefore driven entirely by (1)
the strategy category's own real payoff topology and (2) the
``ExpectedMoveContext`` -- nothing else.

STRATEGY-SPECIFIC CONSTRUCTION, not one generic "spot +/- expected_move"
formula for every payoff topology (this task's own Section 7). Two
distinct evidence-preference orders are used, deliberately, for two
economically distinct purposes:

  - "Sell/reach the expected move" (long options' favorable-side
    target, debit-spread short legs, credit-spread adverse-side
    thresholds, iron condor short strikes): prefers the CURRENT market-
    implied move first (the freshest, most current signal for "how far
    is the market pricing this to move"), historical median move only
    as a documented fallback when implied is unavailable. This mirrors
    a well-established real options-trading heuristic -- selling an
    iron condor's short strikes at the market's own implied-move
    boundary ("selling the expected move") is standard practice, not a
    V4.3 invention.

  - Pinning WIDTH (butterfly/iron-butterfly wings): prefers the
    HISTORICAL median move first -- a real "typical/low-move regime"
    benchmark this task's own Section 14 names explicitly -- because a
    butterfly's economic thesis is a range TIGHTER than whatever the
    market currently happens to be pricing (which is often inflated
    ahead of an earnings event); the current implied move is used only
    as a documented, explicitly-flagged fallback when no historical
    evidence exists.

Neither preference order ever blends or averages the two signals
(Section 4's explicit rule), and neither uses any invented fractional
multiplier -- every boundary is either the full implied move or the
full historical median move, chosen by evidence availability alone,
always disclosed via a reason code.

DELTA IS NOT USED. This task's own Section 18 requires auditing real
delta/Greeks coverage before depending on it; V3's own real chain
provider is documented (strategy_candidates.py's module docstring) as
a narrow, bounded window (e.g. IBKR's ~5-strikes-each-side), which by
itself does not establish reliable delta coverage at the wider,
expected-move-derived targets this module often needs to reach. Per
Section 18's own explicit fallback rule ("if it is not reliable, do
not make strike selection dependent on delta"), this module does not
read the OptionQuote delta field anywhere -- see the V4.3 report's own
delta/Greeks audit section for the evidence this default is based on.

INVALID CONSTRUCTION FAILS HONESTLY (Section 21): every builder below
returns a single ``V4StrikeSelectionResult`` with
``status="unconstructable"`` and real reason codes on any real failure
(insufficient chain, missing leg quote, ordering violation, missing
expected-move evidence) -- never a silent fallback to V3's ATM-index
logic, and never a fabricated strike.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_market_coherence import MarketCoherenceResult
from analytics.decision.v4_strike_resolver import (
    QuoteQuality,
    Right,
    StrikeConstraint,
    next_strike_beyond,
    resolve_target_to_strike,
)
from analytics.options.strategy_candidates import StrategyCategory
from providers.types import OptionQuote

STRIKE_ENGINE_VERSION = "expected_move_v1"

# --------------------------------------------------------------------------
# Reason codes -- explicit, named, never a bare failure with no
# explanation (mirrors v4_compatibility.py's own established pattern).
# --------------------------------------------------------------------------

UNCONSTRUCTABLE_NO_LISTED_STRIKES = "UNCONSTRUCTABLE_NO_LISTED_STRIKES"
UNCONSTRUCTABLE_MISSING_LEG_QUOTE = "UNCONSTRUCTABLE_MISSING_LEG_QUOTE"
UNCONSTRUCTABLE_ORDERING_VIOLATION = "UNCONSTRUCTABLE_ORDERING_VIOLATION"
UNCONSTRUCTABLE_SHARED_CENTER_MISMATCH = "UNCONSTRUCTABLE_SHARED_CENTER_MISMATCH"
UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED = "UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED"
UNCONSTRUCTABLE_NO_PROTECTIVE_WING_AVAILABLE = "UNCONSTRUCTABLE_NO_PROTECTIVE_WING_AVAILABLE"
TARGET_BEYOND_AVAILABLE_CHAIN = "TARGET_BEYOND_AVAILABLE_CHAIN"
BOUNDARY_SOURCE_IMPLIED_MOVE = "BOUNDARY_SOURCE_IMPLIED_MOVE"
BOUNDARY_SOURCE_HISTORICAL_MEDIAN_FALLBACK = "BOUNDARY_SOURCE_HISTORICAL_MEDIAN_FALLBACK"
WIDTH_SOURCE_HISTORICAL_MEDIAN = "WIDTH_SOURCE_HISTORICAL_MEDIAN"
WIDTH_SOURCE_IMPLIED_MOVE_FALLBACK = "WIDTH_SOURCE_IMPLIED_MOVE_FALLBACK"
CHAIN_GRANULARITY_MINIMUM_WING = "CHAIN_GRANULARITY_MINIMUM_WING"
# V4.3.1 -- named pinning-width anchor sources (Section 13), distinct
# from the two above: those are the base-geometry cascade's own
# sources, these are used only by named single-anchor variant
# resolution (pinning_width_boundary_from_anchor).
PINNING_ANCHOR_HISTORICAL_QUARTILE = "PINNING_ANCHOR_HISTORICAL_QUARTILE"
MARKET_COHERENCE_NOT_FRESH = "MARKET_COHERENCE_NOT_FRESH"


# --------------------------------------------------------------------------
# Result types (this task's own Section 20).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class V4Leg:
    action: Literal["buy", "sell"]
    right: Right
    quantity: int
    target_price: Decimal
    target_rationale: str
    selected_strike: Decimal | None
    target_distance_dollars: Decimal | None
    target_distance_pct: Decimal | None
    moneyness_pct: Decimal | None
    expected_move_units: Decimal | None
    external_contract_id: str | None
    quote_quality: QuoteQuality | None
    spread_pct: Decimal | None
    volume: int | None
    open_interest: int | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class V4StrikeSelectionResult:
    strategy: StrategyCategory
    status: Literal["constructed", "unconstructable"]
    spot: Decimal
    expected_move_context: ExpectedMoveContext
    legs: tuple[V4Leg, ...]
    center_target: Decimal | None
    lower_boundary: Decimal | None
    upper_boundary: Decimal | None
    width: Decimal | None
    width_pct_of_spot: Decimal | None
    width_in_expected_move_units: Decimal | None
    symmetry_error_pct: Decimal | None
    reason_codes: tuple[str, ...]
    explanation: str
    engine_version: str


# --------------------------------------------------------------------------
# Expected-move boundary helpers -- see this module's own docstring for
# why these two use opposite evidence-preference orders.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MoveBoundary:
    price: Decimal
    source: str


def expected_move_boundary_at_fraction(
    context: ExpectedMoveContext, side: Literal["up", "down"], em_fraction: Decimal
) -> MoveBoundary | None:
    """V4.3.1 (Section 7) -- the general form of "reach/sell the
    expected move": a target ``em_fraction`` of the full implied move
    (or, as fallback, the full historical median move) away from spot.
    ``em_fraction=1.0`` reproduces V4.3's own base geometry exactly
    (see ``_expected_move_boundary`` below, which is now defined in
    terms of this function -- a pure refactor, zero behavior change,
    confirmed by V4.3's own unmodified 76-test suite still passing).
    Returns None only when neither real signal exists."""
    if context.implied_move_available:
        assert context.implied_move_dollars is not None
        delta = context.implied_move_dollars * em_fraction
        price = context.spot + delta if side == "up" else context.spot - delta
        return MoveBoundary(price=price, source=BOUNDARY_SOURCE_IMPLIED_MOVE)
    if context.historical_median_abs_move_pct is not None:
        delta = context.spot * context.historical_median_abs_move_pct * em_fraction
        price = context.spot + delta if side == "up" else context.spot - delta
        return MoveBoundary(price=price, source=BOUNDARY_SOURCE_HISTORICAL_MEDIAN_FALLBACK)
    return None


def _expected_move_boundary(
    context: ExpectedMoveContext, side: Literal["up", "down"]
) -> MoveBoundary | None:
    """ "Reach/sell the expected move" target -- implied move preferred,
    historical median as the documented fallback. Returns None only
    when neither real signal exists."""
    return expected_move_boundary_at_fraction(context, side, Decimal("1"))


# Named pinning-width anchors (Section 13) -- three separate candidate
# THESES about how wide a "small move" range genuinely is, never
# averaged into one magic number. Each anchor is a real, already-
# computed statistic used at face value (no invented multiplier).
PinningAnchor = Literal["historical_quartile", "historical_median", "implied_move"]


def pinning_width_boundary_from_anchor(
    context: ExpectedMoveContext, side: Literal["up", "down"], anchor: PinningAnchor
) -> MoveBoundary | None:
    """V4.3.1 (Section 13) -- resolves ONE named anchor explicitly,
    never a cascade. ``historical_quartile`` (NARROW_PIN) uses the 25th/
    75th percentile abs-move -- a genuinely tighter, "unsurprising
    outcome" benchmark -- and is only offered when the real sample
    supports it (``historical_quantiles`` is None below
    MIN_N_FOR_QUARTILES, see v4_expected_move.py); this function
    honestly returns None rather than fabricating one. ``historical_median``
    (BASE_HISTORICAL_RANGE) and ``implied_move`` (WIDER_RANGE) mirror
    V4.3's own two existing signals, offered here as explicit,
    independently-selectable variants rather than an automatic
    fallback cascade."""
    if anchor == "historical_quartile":
        quantiles = context.historical_quantiles
        if quantiles is None:
            return None
        # A quartile-width pin is measured from the SAME p75 magnitude
        # on both sides -- the distribution's own spread, not a
        # directional split of p25 vs p75 (which would conflate "how
        # wide" with "which way it tends to lean").
        delta = context.spot * quantiles.p75_abs_move_pct
        price = context.spot + delta if side == "up" else context.spot - delta
        return MoveBoundary(price=price, source=PINNING_ANCHOR_HISTORICAL_QUARTILE)
    if anchor == "historical_median":
        if context.historical_median_abs_move_pct is None:
            return None
        median_price = (
            context.historical_median_upper_boundary
            if side == "up"
            else context.historical_median_lower_boundary
        )
        assert median_price is not None
        return MoveBoundary(price=median_price, source=WIDTH_SOURCE_HISTORICAL_MEDIAN)
    if not context.implied_move_available:
        return None
    implied_price = (
        context.upper_implied_boundary if side == "up" else context.lower_implied_boundary
    )
    assert implied_price is not None
    return MoveBoundary(price=implied_price, source=WIDTH_SOURCE_IMPLIED_MOVE_FALLBACK)


def _pinning_width_boundary(
    context: ExpectedMoveContext, side: Literal["up", "down"]
) -> MoveBoundary | None:
    """Butterfly-wing pinning-width target -- historical median move
    preferred (a real "typical/low-move regime" benchmark), full
    implied move as the documented, explicitly-flagged fallback only
    when no historical evidence exists. Deliberately the OPPOSITE
    preference order from _expected_move_boundary -- see this module's
    docstring. Defined in terms of ``pinning_width_boundary_from_anchor``
    (V4.3.1) -- a pure refactor, zero behavior change."""
    median = pinning_width_boundary_from_anchor(context, side, "historical_median")
    if median is not None:
        return median
    return pinning_width_boundary_from_anchor(context, side, "implied_move")


# --------------------------------------------------------------------------
# Leg construction (wraps the resolver, Section 5/6/15/19).
# --------------------------------------------------------------------------


def _spread_pct_for_report(quote: OptionQuote | None) -> Decimal | None:
    if quote is None or quote.bid is None or quote.ask is None or quote.bid <= 0:
        return None
    return (quote.ask - quote.bid) / quote.bid


def _build_leg(
    action: Literal["buy", "sell"],
    right: Right,
    target_price: Decimal,
    target_rationale: str,
    quotes: list[OptionQuote],
    context: ExpectedMoveContext,
    *,
    quantity: int = 1,
    constraint: StrikeConstraint = StrikeConstraint.NEAREST,
) -> V4Leg:
    resolution = resolve_target_to_strike(target_price, right, quotes, constraint)
    if not resolution.resolvable:
        return V4Leg(
            action=action,
            right=right,
            quantity=quantity,
            target_price=target_price,
            target_rationale=target_rationale,
            selected_strike=None,
            target_distance_dollars=None,
            target_distance_pct=None,
            moneyness_pct=None,
            expected_move_units=None,
            external_contract_id=None,
            quote_quality=None,
            spread_pct=None,
            volume=None,
            open_interest=None,
            reason_codes=(UNCONSTRUCTABLE_NO_LISTED_STRIKES,),
        )

    strike = resolution.selected_strike
    assert strike is not None
    reason_codes = [TARGET_BEYOND_AVAILABLE_CHAIN] if resolution.hit_chain_edge else []
    moneyness = (strike - context.spot) / context.spot if context.spot != 0 else None
    move_units = (
        (strike - context.spot) / context.implied_move_dollars
        if context.implied_move_dollars
        else None
    )
    quote = resolution.quote
    return V4Leg(
        action=action,
        right=right,
        quantity=quantity,
        target_price=target_price,
        target_rationale=target_rationale,
        selected_strike=strike,
        target_distance_dollars=resolution.distance_dollars,
        target_distance_pct=resolution.distance_pct,
        moneyness_pct=moneyness,
        expected_move_units=move_units,
        external_contract_id=resolution.external_contract_id,
        quote_quality=resolution.quote_quality,
        spread_pct=_spread_pct_for_report(quote),
        volume=quote.volume if quote else None,
        open_interest=quote.open_interest if quote else None,
        reason_codes=tuple(reason_codes),
    )


def _fmt(value: Decimal | None, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if value is not None else "n/a"


def _unconstructable(
    strategy: StrategyCategory,
    context: ExpectedMoveContext,
    reason_codes: list[str],
    explanation: str,
    legs: tuple[V4Leg, ...] = (),
) -> V4StrikeSelectionResult:
    return V4StrikeSelectionResult(
        strategy=strategy,
        status="unconstructable",
        spot=context.spot,
        expected_move_context=context,
        legs=legs,
        center_target=None,
        lower_boundary=None,
        upper_boundary=None,
        width=None,
        width_pct_of_spot=None,
        width_in_expected_move_units=None,
        symmetry_error_pct=None,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        explanation=explanation,
        engine_version=STRIKE_ENGINE_VERSION,
    )


def _symmetry_error_pct(upper_distance: Decimal, lower_distance: Decimal, spot: Decimal) -> Decimal:
    if spot == 0:
        return Decimal(0)
    return abs(upper_distance - lower_distance) / spot


# --------------------------------------------------------------------------
# V4.3.1 public re-exports -- v4_strike_geometry_variants.py needs the
# SAME leg-construction/result-building primitives the base geometry
# below uses, so variant geometries are built the identical way, never
# a second, potentially-inconsistent implementation. Thin wrappers
# over the private helpers above; zero behavior change to either.
# --------------------------------------------------------------------------


def build_leg(
    action: Literal["buy", "sell"],
    right: Right,
    target_price: Decimal,
    target_rationale: str,
    quotes: list[OptionQuote],
    context: ExpectedMoveContext,
    *,
    quantity: int = 1,
    constraint: StrikeConstraint = StrikeConstraint.NEAREST,
) -> V4Leg:
    return _build_leg(
        action,
        right,
        target_price,
        target_rationale,
        quotes,
        context,
        quantity=quantity,
        constraint=constraint,
    )


def unconstructable_result(
    strategy: StrategyCategory,
    context: ExpectedMoveContext,
    reason_codes: list[str],
    explanation: str,
    legs: tuple[V4Leg, ...] = (),
) -> V4StrikeSelectionResult:
    return _unconstructable(strategy, context, reason_codes, explanation, legs)


def symmetry_error_pct(upper_distance: Decimal, lower_distance: Decimal, spot: Decimal) -> Decimal:
    return _symmetry_error_pct(upper_distance, lower_distance, spot)


def fmt_decimal(value: Decimal | None, digits: int = 2) -> str:
    return _fmt(value, digits)


# --------------------------------------------------------------------------
# Per-strategy construction (Sections 7-14). Each strategy family gets
# its own function -- deliberately never one shared "spot +/- move"
# formula.
# --------------------------------------------------------------------------


def _build_long_straddle(
    context: ExpectedMoveContext, quotes: list[OptionQuote]
) -> V4StrikeSelectionResult:
    target = context.spot
    rationale = (
        "Nearest coherent-spot strike -- shared by both legs of a two-sided convex structure."
    )
    call_leg = _build_leg("buy", "call", target, rationale, quotes, context)
    put_leg = _build_leg("buy", "put", target, rationale, quotes, context)
    if call_leg.selected_strike is None or put_leg.selected_strike is None:
        return _unconstructable(
            StrategyCategory.LONG_STRADDLE,
            context,
            [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
            "Real chain lacks a quoted call or put near coherent spot.",
            legs=(call_leg, put_leg),
        )
    if call_leg.selected_strike != put_leg.selected_strike:
        return _unconstructable(
            StrategyCategory.LONG_STRADDLE,
            context,
            [UNCONSTRUCTABLE_SHARED_CENTER_MISMATCH],
            f"Nearest-ATM call strike {call_leg.selected_strike} and put strike "
            f"{put_leg.selected_strike} differ -- this chain cannot honestly support a "
            "straddle at this expiration.",
            legs=(call_leg, put_leg),
        )
    center = call_leg.selected_strike
    reason_codes = list(call_leg.reason_codes) + list(put_leg.reason_codes)
    return V4StrikeSelectionResult(
        strategy=StrategyCategory.LONG_STRADDLE,
        status="constructed",
        spot=context.spot,
        expected_move_context=context,
        legs=(call_leg, put_leg),
        center_target=target,
        lower_boundary=None,
        upper_boundary=None,
        width=Decimal(0),
        width_pct_of_spot=Decimal(0),
        width_in_expected_move_units=Decimal(0),
        symmetry_error_pct=Decimal(0),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        explanation=f"Long straddle centered at real ATM strike {center} (spot {context.spot}).",
        engine_version=STRIKE_ENGINE_VERSION,
    )


def _build_long_strangle(
    context: ExpectedMoveContext, quotes: list[OptionQuote]
) -> V4StrikeSelectionResult:
    call_boundary = _expected_move_boundary(context, "up")
    put_boundary = _expected_move_boundary(context, "down")
    if call_boundary is None or put_boundary is None:
        return _unconstructable(
            StrategyCategory.LONG_STRANGLE,
            context,
            [UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED],
            "Neither implied move nor adequate historical move data is available to place "
            "expected-move-aware strangle strikes.",
        )
    call_leg = _build_leg(
        "buy",
        "call",
        call_boundary.price,
        f"Full upper expected-move boundary ({call_boundary.source}) -- the market's own "
        "expected move used at face value, no invented fraction.",
        quotes,
        context,
    )
    put_leg = _build_leg(
        "buy",
        "put",
        put_boundary.price,
        f"Full lower expected-move boundary ({put_boundary.source}) -- the market's own "
        "expected move used at face value, no invented fraction.",
        quotes,
        context,
    )
    if call_leg.selected_strike is None or put_leg.selected_strike is None:
        return _unconstructable(
            StrategyCategory.LONG_STRANGLE,
            context,
            [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
            "Real chain lacks a quoted call or put near the expected-move boundary.",
            legs=(call_leg, put_leg),
        )
    if not (put_leg.selected_strike < context.spot < call_leg.selected_strike):
        return _unconstructable(
            StrategyCategory.LONG_STRANGLE,
            context,
            [UNCONSTRUCTABLE_ORDERING_VIOLATION],
            f"Resolved put strike {put_leg.selected_strike} / call strike "
            f"{call_leg.selected_strike} do not straddle spot {context.spot}.",
            legs=(call_leg, put_leg),
        )
    upper_distance = call_leg.selected_strike - context.spot
    lower_distance = context.spot - put_leg.selected_strike
    width = call_leg.selected_strike - put_leg.selected_strike
    reason_codes = [call_boundary.source, put_boundary.source]
    reason_codes += list(call_leg.reason_codes) + list(put_leg.reason_codes)
    return V4StrikeSelectionResult(
        strategy=StrategyCategory.LONG_STRANGLE,
        status="constructed",
        spot=context.spot,
        expected_move_context=context,
        legs=(call_leg, put_leg),
        center_target=context.spot,
        lower_boundary=put_leg.selected_strike,
        upper_boundary=call_leg.selected_strike,
        width=width,
        width_pct_of_spot=width / context.spot if context.spot else None,
        width_in_expected_move_units=(
            width / context.implied_move_dollars if context.implied_move_dollars else None
        ),
        symmetry_error_pct=_symmetry_error_pct(upper_distance, lower_distance, context.spot),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        explanation=(
            f"Long strangle: put target {_fmt(put_boundary.price)} ({put_boundary.source}) -> "
            f"strike {put_leg.selected_strike}; call target {_fmt(call_boundary.price)} "
            f"({call_boundary.source}) -> strike {call_leg.selected_strike}."
        ),
        engine_version=STRIKE_ENGINE_VERSION,
    )


def _build_directional_long(
    category: StrategyCategory, right: Right, side: Literal["up", "down"]
) -> Callable[[ExpectedMoveContext, list[OptionQuote]], V4StrikeSelectionResult]:
    def _build(context: ExpectedMoveContext, quotes: list[OptionQuote]) -> V4StrikeSelectionResult:
        boundary = _expected_move_boundary(context, side)
        if boundary is None:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED],
                "Neither implied move nor adequate historical move data is available to "
                "place an expected-move-aware directional target.",
            )
        leg = _build_leg(
            "buy",
            right,
            boundary.price,
            f"Expected-move boundary ({boundary.source}) in the favorable direction -- "
            "under the T+1 liquidation objective (exit ~1 session later, not at "
            "expiration) this is the point of maximum linear sensitivity to a materialized "
            "move, not merely 'reachable by expiration'.",
            quotes,
            context,
        )
        if leg.selected_strike is None:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
                f"Real chain has no quoted {right} near the expected-move target.",
                legs=(leg,),
            )
        reason_codes = [boundary.source] + list(leg.reason_codes)
        return V4StrikeSelectionResult(
            strategy=category,
            status="constructed",
            spot=context.spot,
            expected_move_context=context,
            legs=(leg,),
            center_target=boundary.price,
            lower_boundary=leg.selected_strike if side == "down" else None,
            upper_boundary=leg.selected_strike if side == "up" else None,
            width=None,
            width_pct_of_spot=None,
            width_in_expected_move_units=leg.expected_move_units,
            symmetry_error_pct=None,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            explanation=(
                f"Target {_fmt(boundary.price)} ({boundary.source}) resolved to real strike "
                f"{leg.selected_strike} ({_fmt(leg.expected_move_units)} expected-move units "
                "from spot)."
            ),
            engine_version=STRIKE_ENGINE_VERSION,
        )

    return _build


_build_long_call = _build_directional_long(StrategyCategory.LONG_CALL, "call", "up")
_build_long_put = _build_directional_long(StrategyCategory.LONG_PUT, "put", "down")


def _build_debit_spread(
    category: StrategyCategory, right: Right, side: Literal["up", "down"]
) -> Callable[[ExpectedMoveContext, list[OptionQuote]], V4StrikeSelectionResult]:
    def _build(context: ExpectedMoveContext, quotes: list[OptionQuote]) -> V4StrikeSelectionResult:
        boundary = _expected_move_boundary(context, side)
        if boundary is None:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED],
                "Neither implied move nor adequate historical move data is available to "
                "place the short leg's expected-move target.",
            )
        long_leg = _build_leg(
            "buy", right, context.spot, "ATM directional anchor.", quotes, context
        )
        short_leg = _build_leg(
            "sell",
            right,
            boundary.price,
            f"Expected-move target ({boundary.source}) -- defines the capped profit region.",
            quotes,
            context,
        )
        if long_leg.selected_strike is None or short_leg.selected_strike is None:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
                "Real chain lacks a quoted long or short leg.",
                legs=(long_leg, short_leg),
            )
        ordered = (
            long_leg.selected_strike < short_leg.selected_strike
            if side == "up"
            else long_leg.selected_strike > short_leg.selected_strike
        )
        if not ordered:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_ORDERING_VIOLATION],
                f"Long strike {long_leg.selected_strike} / short strike "
                f"{short_leg.selected_strike} produce a zero-width or reversed spread.",
                legs=(long_leg, short_leg),
            )
        width = abs(short_leg.selected_strike - long_leg.selected_strike)
        reason_codes = (
            [boundary.source] + list(long_leg.reason_codes) + list(short_leg.reason_codes)
        )
        return V4StrikeSelectionResult(
            strategy=category,
            status="constructed",
            spot=context.spot,
            expected_move_context=context,
            legs=(long_leg, short_leg),
            center_target=context.spot,
            lower_boundary=min(long_leg.selected_strike, short_leg.selected_strike),
            upper_boundary=max(long_leg.selected_strike, short_leg.selected_strike),
            width=width,
            width_pct_of_spot=width / context.spot if context.spot else None,
            width_in_expected_move_units=(
                width / context.implied_move_dollars if context.implied_move_dollars else None
            ),
            symmetry_error_pct=None,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            explanation=(
                f"Long leg ATM at {long_leg.selected_strike}; short leg target "
                f"{_fmt(boundary.price)} ({boundary.source}) -> strike "
                f"{short_leg.selected_strike}, width {width}."
            ),
            engine_version=STRIKE_ENGINE_VERSION,
        )

    return _build


_build_bull_call_spread = _build_debit_spread(StrategyCategory.BULL_CALL_SPREAD, "call", "up")
_build_bear_put_spread = _build_debit_spread(StrategyCategory.BEAR_PUT_SPREAD, "put", "down")


def _build_credit_spread(
    category: StrategyCategory, right: Right, side: Literal["up", "down"]
) -> Callable[[ExpectedMoveContext, list[OptionQuote]], V4StrikeSelectionResult]:
    def _build(context: ExpectedMoveContext, quotes: list[OptionQuote]) -> V4StrikeSelectionResult:
        boundary = _expected_move_boundary(context, side)
        if boundary is None:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED],
                "Neither implied move nor adequate historical move data is available to "
                "place the short strike's adverse-move threshold.",
            )
        short_leg = _build_leg(
            "sell",
            right,
            boundary.price,
            f"Adverse-move threshold ({boundary.source}) -- a directional threshold, not a "
            "pinning center (V4.2's own correction): profits identically whether the "
            "underlying finishes just past this strike or far beyond it favorably.",
            quotes,
            context,
        )
        if short_leg.selected_strike is None:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
                "Real chain lacks a quoted short leg near the adverse-move threshold.",
                legs=(short_leg,),
            )
        wing_direction: Literal["up", "down"] = "down" if side == "down" else "up"
        long_strike = next_strike_beyond(quotes, right, short_leg.selected_strike, wing_direction)
        if long_strike is None:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_NO_PROTECTIVE_WING_AVAILABLE],
                f"No further-OTM real {right} strike exists beyond the short strike "
                f"{short_leg.selected_strike} to build a protective wing.",
                legs=(short_leg,),
            )
        long_leg = _build_leg(
            "buy",
            right,
            long_strike,
            "Next real listed strike beyond the short strike -- minimum viable protective "
            "wing; real risk-sizing of wing width is V4.4's job, not V4.3's.",
            quotes,
            context,
        )
        if long_leg.selected_strike is None:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
                "Real chain lacks a quote at the protective-wing strike.",
                legs=(short_leg, long_leg),
            )
        ordered = (
            long_leg.selected_strike < short_leg.selected_strike
            if side == "down"
            else short_leg.selected_strike < long_leg.selected_strike
        )
        if not ordered:
            return _unconstructable(
                category,
                context,
                [UNCONSTRUCTABLE_ORDERING_VIOLATION],
                f"Protective wing {long_leg.selected_strike} does not lie beyond short "
                f"strike {short_leg.selected_strike}.",
                legs=(short_leg, long_leg),
            )
        width = abs(short_leg.selected_strike - long_leg.selected_strike)
        reason_codes = (
            [boundary.source, CHAIN_GRANULARITY_MINIMUM_WING]
            + list(short_leg.reason_codes)
            + list(long_leg.reason_codes)
        )
        return V4StrikeSelectionResult(
            strategy=category,
            status="constructed",
            spot=context.spot,
            expected_move_context=context,
            legs=(short_leg, long_leg),
            center_target=None,
            lower_boundary=min(long_leg.selected_strike, short_leg.selected_strike),
            upper_boundary=max(long_leg.selected_strike, short_leg.selected_strike),
            width=width,
            width_pct_of_spot=width / context.spot if context.spot else None,
            width_in_expected_move_units=(
                width / context.implied_move_dollars if context.implied_move_dollars else None
            ),
            symmetry_error_pct=None,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            explanation=(
                f"Short strike target {_fmt(boundary.price)} ({boundary.source}) -> strike "
                f"{short_leg.selected_strike}; protective wing at next listed strike "
                f"{long_leg.selected_strike}, width {width}."
            ),
            engine_version=STRIKE_ENGINE_VERSION,
        )

    return _build


_build_put_credit_spread = _build_credit_spread(StrategyCategory.PUT_CREDIT_SPREAD, "put", "down")
_build_call_credit_spread = _build_credit_spread(StrategyCategory.CALL_CREDIT_SPREAD, "call", "up")


def _build_iron_condor(
    context: ExpectedMoveContext, quotes: list[OptionQuote]
) -> V4StrikeSelectionResult:
    call_boundary = _expected_move_boundary(context, "up")
    put_boundary = _expected_move_boundary(context, "down")
    if call_boundary is None or put_boundary is None:
        return _unconstructable(
            StrategyCategory.IRON_CONDOR,
            context,
            [UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED],
            "Neither implied move nor adequate historical move data is available to place "
            "the range boundaries.",
        )
    short_put = _build_leg(
        "sell",
        "put",
        put_boundary.price,
        f"Lower range boundary ({put_boundary.source}) -- 'selling the expected move', "
        "standard real practice for condor short-strike placement.",
        quotes,
        context,
    )
    short_call = _build_leg(
        "sell",
        "call",
        call_boundary.price,
        f"Upper range boundary ({call_boundary.source}) -- 'selling the expected move'.",
        quotes,
        context,
    )
    if short_put.selected_strike is None or short_call.selected_strike is None:
        return _unconstructable(
            StrategyCategory.IRON_CONDOR,
            context,
            [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
            "Real chain lacks a quoted short put or short call near the range boundary.",
            legs=(short_put, short_call),
        )
    long_put_strike = next_strike_beyond(quotes, "put", short_put.selected_strike, "down")
    long_call_strike = next_strike_beyond(quotes, "call", short_call.selected_strike, "up")
    if long_put_strike is None or long_call_strike is None:
        return _unconstructable(
            StrategyCategory.IRON_CONDOR,
            context,
            [UNCONSTRUCTABLE_NO_PROTECTIVE_WING_AVAILABLE],
            "No further-OTM real strike exists to build one or both protective wings.",
            legs=(short_put, short_call),
        )
    long_put = _build_leg(
        "buy",
        "put",
        long_put_strike,
        "Next real listed strike beyond the short put -- minimum viable protective wing.",
        quotes,
        context,
    )
    long_call = _build_leg(
        "buy",
        "call",
        long_call_strike,
        "Next real listed strike beyond the short call -- minimum viable protective wing.",
        quotes,
        context,
    )
    if long_put.selected_strike is None or long_call.selected_strike is None:
        return _unconstructable(
            StrategyCategory.IRON_CONDOR,
            context,
            [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
            "Real chain lacks a quote at one or both protective-wing strikes.",
            legs=(short_put, short_call, long_put, long_call),
        )
    if not (
        long_put.selected_strike
        < short_put.selected_strike
        < short_call.selected_strike
        < long_call.selected_strike
    ):
        return _unconstructable(
            StrategyCategory.IRON_CONDOR,
            context,
            [UNCONSTRUCTABLE_ORDERING_VIOLATION],
            f"Resolved strikes {long_put.selected_strike} < {short_put.selected_strike} < "
            f"{short_call.selected_strike} < {long_call.selected_strike} do not hold.",
            legs=(short_put, short_call, long_put, long_call),
        )
    upper_distance = short_call.selected_strike - context.spot
    lower_distance = context.spot - short_put.selected_strike
    range_width = short_call.selected_strike - short_put.selected_strike
    reason_codes = (
        [call_boundary.source, put_boundary.source, CHAIN_GRANULARITY_MINIMUM_WING]
        + list(short_put.reason_codes)
        + list(short_call.reason_codes)
        + list(long_put.reason_codes)
        + list(long_call.reason_codes)
    )
    return V4StrikeSelectionResult(
        strategy=StrategyCategory.IRON_CONDOR,
        status="constructed",
        spot=context.spot,
        expected_move_context=context,
        legs=(long_put, short_put, short_call, long_call),
        center_target=context.spot,
        lower_boundary=short_put.selected_strike,
        upper_boundary=short_call.selected_strike,
        width=range_width,
        width_pct_of_spot=range_width / context.spot if context.spot else None,
        width_in_expected_move_units=(
            range_width / context.implied_move_dollars if context.implied_move_dollars else None
        ),
        symmetry_error_pct=_symmetry_error_pct(upper_distance, lower_distance, context.spot),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        explanation=(
            f"Market expected range: [{short_put.selected_strike}, {short_call.selected_strike}] "
            f"(targets {_fmt(put_boundary.price)}/{_fmt(call_boundary.price)}, "
            f"{put_boundary.source}/{call_boundary.source}). Protective wings at "
            f"{long_put.selected_strike}/{long_call.selected_strike}."
        ),
        engine_version=STRIKE_ENGINE_VERSION,
    )


def _build_long_call_butterfly(
    context: ExpectedMoveContext, quotes: list[OptionQuote]
) -> V4StrikeSelectionResult:
    category = StrategyCategory.LONG_CALL_BUTTERFLY
    upper_boundary = _pinning_width_boundary(context, "up")
    lower_boundary = _pinning_width_boundary(context, "down")
    if upper_boundary is None or lower_boundary is None:
        return _unconstructable(
            category,
            context,
            [UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED],
            "Neither historical median move nor implied move is available to size the "
            "pinning wing width.",
        )
    # A 1-2-1 call butterfly sells the middle strike at quantity=2.
    center_leg = _build_leg(
        "sell",
        "call",
        context.spot,
        "ATM center -- the pinning target.",
        quotes,
        context,
        quantity=2,
    )
    lower_leg = _build_leg(
        "buy",
        "call",
        lower_boundary.price,
        f"Lower wing ({lower_boundary.source}).",
        quotes,
        context,
    )
    upper_leg = _build_leg(
        "buy",
        "call",
        upper_boundary.price,
        f"Upper wing ({upper_boundary.source}).",
        quotes,
        context,
    )
    if any(leg.selected_strike is None for leg in (center_leg, lower_leg, upper_leg)):
        return _unconstructable(
            category,
            context,
            [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
            "Real chain lacks a quoted center or wing leg.",
            legs=(lower_leg, center_leg, upper_leg),
        )
    assert center_leg.selected_strike is not None
    assert lower_leg.selected_strike is not None
    assert upper_leg.selected_strike is not None
    if not (lower_leg.selected_strike < center_leg.selected_strike < upper_leg.selected_strike):
        return _unconstructable(
            category,
            context,
            [UNCONSTRUCTABLE_ORDERING_VIOLATION],
            f"Resolved strikes {lower_leg.selected_strike} < {center_leg.selected_strike} "
            f"< {upper_leg.selected_strike} do not hold -- a wing overlaps the center.",
            legs=(lower_leg, center_leg, upper_leg),
        )
    upper_distance = upper_leg.selected_strike - center_leg.selected_strike
    lower_distance = center_leg.selected_strike - lower_leg.selected_strike
    width = upper_leg.selected_strike - lower_leg.selected_strike
    reason_codes = (
        [upper_boundary.source, lower_boundary.source]
        + list(center_leg.reason_codes)
        + list(lower_leg.reason_codes)
        + list(upper_leg.reason_codes)
    )
    return V4StrikeSelectionResult(
        strategy=category,
        status="constructed",
        spot=context.spot,
        expected_move_context=context,
        legs=(lower_leg, center_leg, upper_leg),
        center_target=context.spot,
        lower_boundary=lower_leg.selected_strike,
        upper_boundary=upper_leg.selected_strike,
        width=width,
        width_pct_of_spot=width / context.spot if context.spot else None,
        width_in_expected_move_units=(
            width / context.implied_move_dollars if context.implied_move_dollars else None
        ),
        symmetry_error_pct=_symmetry_error_pct(upper_distance, lower_distance, context.spot),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        explanation=(
            f"Center target {context.spot} -> strike {center_leg.selected_strike}. Wing "
            f"targets {_fmt(lower_boundary.price)}/{_fmt(upper_boundary.price)} "
            f"({lower_boundary.source}/{upper_boundary.source}) -> strikes "
            f"{lower_leg.selected_strike}/{upper_leg.selected_strike}, width {width}."
        ),
        engine_version=STRIKE_ENGINE_VERSION,
    )


def _build_iron_butterfly(
    context: ExpectedMoveContext, quotes: list[OptionQuote]
) -> V4StrikeSelectionResult:
    upper_boundary = _pinning_width_boundary(context, "up")
    lower_boundary = _pinning_width_boundary(context, "down")
    if upper_boundary is None or lower_boundary is None:
        return _unconstructable(
            StrategyCategory.IRON_BUTTERFLY,
            context,
            [UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED],
            "Neither historical median move nor implied move is available to size the "
            "pinning wing width.",
        )
    short_put = _build_leg("sell", "put", context.spot, "ATM center (put leg).", quotes, context)
    short_call = _build_leg("sell", "call", context.spot, "ATM center (call leg).", quotes, context)
    if short_put.selected_strike is None or short_call.selected_strike is None:
        return _unconstructable(
            StrategyCategory.IRON_BUTTERFLY,
            context,
            [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
            "Real chain lacks a quoted ATM put or call.",
            legs=(short_put, short_call),
        )
    if short_put.selected_strike != short_call.selected_strike:
        return _unconstructable(
            StrategyCategory.IRON_BUTTERFLY,
            context,
            [UNCONSTRUCTABLE_SHARED_CENTER_MISMATCH],
            f"Nearest-ATM put strike {short_put.selected_strike} and call strike "
            f"{short_call.selected_strike} differ -- cannot honestly support a shared "
            "center at this expiration.",
            legs=(short_put, short_call),
        )
    center = short_put.selected_strike
    long_put = _build_leg(
        "buy",
        "put",
        lower_boundary.price,
        f"Lower wing ({lower_boundary.source}).",
        quotes,
        context,
    )
    long_call = _build_leg(
        "buy",
        "call",
        upper_boundary.price,
        f"Upper wing ({upper_boundary.source}).",
        quotes,
        context,
    )
    if long_put.selected_strike is None or long_call.selected_strike is None:
        return _unconstructable(
            StrategyCategory.IRON_BUTTERFLY,
            context,
            [UNCONSTRUCTABLE_MISSING_LEG_QUOTE],
            "Real chain lacks a quoted protective wing.",
            legs=(long_put, short_put, short_call, long_call),
        )
    if not (long_put.selected_strike < center < long_call.selected_strike):
        return _unconstructable(
            StrategyCategory.IRON_BUTTERFLY,
            context,
            [UNCONSTRUCTABLE_ORDERING_VIOLATION],
            f"Resolved strikes {long_put.selected_strike} < {center} < "
            f"{long_call.selected_strike} do not hold.",
            legs=(long_put, short_put, short_call, long_call),
        )
    upper_distance = long_call.selected_strike - center
    lower_distance = center - long_put.selected_strike
    width = long_call.selected_strike - long_put.selected_strike
    reason_codes = (
        [upper_boundary.source, lower_boundary.source]
        + list(short_put.reason_codes)
        + list(short_call.reason_codes)
        + list(long_put.reason_codes)
        + list(long_call.reason_codes)
    )
    return V4StrikeSelectionResult(
        strategy=StrategyCategory.IRON_BUTTERFLY,
        status="constructed",
        spot=context.spot,
        expected_move_context=context,
        legs=(long_put, short_put, short_call, long_call),
        center_target=context.spot,
        lower_boundary=long_put.selected_strike,
        upper_boundary=long_call.selected_strike,
        width=width,
        width_pct_of_spot=width / context.spot if context.spot else None,
        width_in_expected_move_units=(
            width / context.implied_move_dollars if context.implied_move_dollars else None
        ),
        symmetry_error_pct=_symmetry_error_pct(upper_distance, lower_distance, context.spot),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        explanation=(
            f"Shared center {center} (spot {context.spot}). Wing targets "
            f"{_fmt(lower_boundary.price)}/{_fmt(upper_boundary.price)} "
            f"({lower_boundary.source}/{upper_boundary.source}) -> strikes "
            f"{long_put.selected_strike}/{long_call.selected_strike}, width {width}."
        ),
        engine_version=STRIKE_ENGINE_VERSION,
    )


_BUILDERS: dict[
    StrategyCategory, Callable[[ExpectedMoveContext, list[OptionQuote]], V4StrikeSelectionResult]
] = {
    StrategyCategory.LONG_CALL: _build_long_call,
    StrategyCategory.LONG_PUT: _build_long_put,
    StrategyCategory.BULL_CALL_SPREAD: _build_bull_call_spread,
    StrategyCategory.BEAR_PUT_SPREAD: _build_bear_put_spread,
    StrategyCategory.PUT_CREDIT_SPREAD: _build_put_credit_spread,
    StrategyCategory.CALL_CREDIT_SPREAD: _build_call_credit_spread,
    StrategyCategory.LONG_STRADDLE: _build_long_straddle,
    StrategyCategory.LONG_STRANGLE: _build_long_strangle,
    StrategyCategory.IRON_CONDOR: _build_iron_condor,
    StrategyCategory.LONG_CALL_BUTTERFLY: _build_long_call_butterfly,
    StrategyCategory.IRON_BUTTERFLY: _build_iron_butterfly,
}


def select_v4_strikes(
    strategy: StrategyCategory,
    context: ExpectedMoveContext,
    quotes: list[OptionQuote],
    market_coherence: MarketCoherenceResult | None = None,
) -> V4StrikeSelectionResult:
    """The one dispatcher every V4.3 caller uses -- each strategy family
    has its OWN construction (Section 7), never a single generic
    formula. ``market_coherence`` (V4.2's market-coherence policy
    foundation, Section 30) is surfaced via a reason code when
    non-fresh -- never used to reject or alter construction; V4.2's own
    "no rejection activated" rule extends unchanged into V4.3."""
    result = _BUILDERS[strategy](context, quotes)
    if market_coherence is not None and market_coherence.status != "fresh":
        result = replace(
            result,
            reason_codes=tuple(dict.fromkeys((*result.reason_codes, MARKET_COHERENCE_NOT_FRESH))),
        )
    return result
