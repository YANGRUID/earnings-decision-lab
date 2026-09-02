"""Options Decision Engine V4 -- Strategy Semantics Registry (V4.1,
corrected in V4.2, 2026-08-31/09-01).

The forensic audit's central, confirmed finding: V3's ``_volatility_fit``
(analytics/decision/strategy_scoring.py) scores LONG_VOL purely on the
*sign* of a candidate's net_premium -- full weight to any net-debit
structure. That conflates "costs a debit" with "wants a large realized
move," which is true for a long call/put/straddle/strangle but false for
a long call butterfly: a 1-2-1 butterfly is a net-debit structure whose
maximum profit sits at its center strike and decays to a full loss on
*either* side -- economically a pinning/small-move bet wearing a debit
position's clothing. Three of V3's seven real settled losses (CRM, VEEV,
NVDA) trace directly to this mislabeling: a LONG_VOL recommendation that
was arguably *right about the market* (all three moved dramatically) and
still lost, because the structure chosen to express that view profits
from the opposite.

This module is the authoritative, deterministic classification of what
each real strategy family actually pays off on -- never derived from
debit/credit sign alone, always from the real payoff geometry
(analytics/options/payoff.py). It is READ-ONLY, additive metadata:

    V3 DOES NOT DEPEND ON THIS MODULE. strategy_scoring.py, budget.py,
    and every other real V3 ranking/sizing code path are untouched by
    this file's existence -- V3's own _volatility_fit still runs exactly
    as before. This registry only becomes load-bearing via V4.2's own
    compatibility engine (analytics/decision/v4_compatibility.py).

V4.2 RE-AUDIT CORRECTION (2026-09-01): the V4.1 registry classified
PUT_CREDIT_SPREAD/CALL_CREDIT_SPREAD move_intent as
``small_move_pinning`` -- the same value used for the butterflies. That
conflated two genuinely different structures: a butterfly needs the
underlying to finish NEAR one specific center strike (moving too far in
EITHER direction hurts it equally), while a credit spread only needs the
underlying to avoid crossing ONE threshold strike -- it is exactly as
happy whether the underlying finishes near the short strike or far
beyond it in the favorable direction. This is a real, distinct payoff
shape (a one-sided threshold, not a two-sided peak), now given its own
``directional_threshold`` value. Every other V4.1 classification was
re-checked against actual payoff geometry (analytics/options/payoff.py)
and confirmed correct -- see the strike-by-strike audit trail in each
entry's ``rationale`` below.

Every classification is grounded in the real, closed-form payoff shape
of each family (analytics/options/payoff.py::analyze), not in intent or
naming -- see each dimension's own docstring for the exact economic
reasoning.
"""

from dataclasses import dataclass
from typing import Literal

from analytics.options.strategy_candidates import StrategyCategory

STRATEGY_SEMANTICS_VERSION = "v4-strategy-semantics-v2"

DirectionalIntent = Literal["bullish", "bearish", "neutral_range", "direction_agnostic"]

# V4.2: added "directional_threshold" -- see the module docstring's
# re-audit correction. A butterfly's "small_move_pinning" (must finish
# NEAR one center point) and a credit spread's "directional_threshold"
# (must avoid crossing ONE boundary, any distance past it on the
# favorable side is equally fine) are not the same shape.
MoveMagnitudeIntent = Literal[
    "large_move", "moderate_move", "small_move_pinning", "range_bound", "directional_threshold"
]

VolatilityIntent = Literal[
    "long_realized_move",
    "short_realized_move",
    "mixed_path_dependent",
]

PayoffShape = Literal[
    "single_sided_convex",
    "vertical_bounded_directional",
    "tent_pinning",
    "range_credit",
    "two_sided_convex",
]


@dataclass(frozen=True)
class StrategySemantics:
    """The real economic behavior of one strategy family -- every field
    is a claim about the family's actual payoff geometry, never about
    its debit/credit sign or its name.

    ``failure_modes`` (V4.2, this task's own Section 1E) -- the real,
    named ways this family typically loses, used by
    analytics/decision/v4_compatibility.py to build human-readable
    contradiction explanations. Descriptive, not exhaustive."""

    category: StrategyCategory
    directional_intent: DirectionalIntent
    move_intent: MoveMagnitudeIntent
    volatility_intent: VolatilityIntent
    payoff_shape: PayoffShape
    failure_modes: tuple[str, ...]
    rationale: str


_REGISTRY: dict[StrategyCategory, StrategySemantics] = {
    StrategyCategory.LONG_CALL: StrategySemantics(
        category=StrategyCategory.LONG_CALL,
        directional_intent="bullish",
        move_intent="large_move",
        volatility_intent="long_realized_move",
        payoff_shape="single_sided_convex",
        failure_modes=("wrong_direction", "move_too_small_premium_not_recovered", "iv_crush"),
        rationale=(
            "Unbounded, convex upside past the strike -- profits increasingly with a larger "
            "up-move, and a genuinely large realized move is what a long call is economically "
            "for. Correctly LONG_VOL under debit-sign scoring too: this is the one family "
            "where that shortcut happens to give the right answer. But debit sign alone is "
            "not sufficient (V4.2 Section 11) -- it ALSO needs real directional compatibility; "
            "a long call is not a good NEUTRAL+LONG_VOL fit merely because it is convex."
        ),
    ),
    StrategyCategory.LONG_PUT: StrategySemantics(
        category=StrategyCategory.LONG_PUT,
        directional_intent="bearish",
        move_intent="large_move",
        volatility_intent="long_realized_move",
        payoff_shape="single_sided_convex",
        failure_modes=("wrong_direction", "move_too_small_premium_not_recovered", "iv_crush"),
        rationale="Mirror of LONG_CALL on the downside -- bounded max profit (S can't go "
        "below 0) but the same convex, large-move-seeking economics, and the same "
        "requirement for real bearish directional compatibility, not just convexity.",
    ),
    StrategyCategory.BULL_CALL_SPREAD: StrategySemantics(
        category=StrategyCategory.BULL_CALL_SPREAD,
        directional_intent="bullish",
        move_intent="moderate_move",
        volatility_intent="mixed_path_dependent",
        payoff_shape="vertical_bounded_directional",
        failure_modes=(
            "wrong_direction",
            "move_too_small_to_clear_short_strike",
            "spread_friction",
        ),
        rationale="A real directional bet, but the short leg caps both the profit and the "
        "position's own vega -- once the underlying clears the short strike, further "
        "movement (or higher realized vol) no longer helps. Not cleanly 'wants a big move' "
        "the way a naked long or a straddle does; distinct from LONG_CALL, which retains "
        "unbounded convexity and needs to recover the full premium alone.",
    ),
    StrategyCategory.BEAR_PUT_SPREAD: StrategySemantics(
        category=StrategyCategory.BEAR_PUT_SPREAD,
        directional_intent="bearish",
        move_intent="moderate_move",
        volatility_intent="mixed_path_dependent",
        payoff_shape="vertical_bounded_directional",
        failure_modes=(
            "wrong_direction",
            "move_too_small_to_clear_short_strike",
            "spread_friction",
        ),
        rationale="Mirror of BULL_CALL_SPREAD on the downside -- same capped, "
        "path-dependent economics, distinct from LONG_PUT's unbounded convexity.",
    ),
    StrategyCategory.PUT_CREDIT_SPREAD: StrategySemantics(
        category=StrategyCategory.PUT_CREDIT_SPREAD,
        directional_intent="bullish",
        move_intent="directional_threshold",
        volatility_intent="short_realized_move",
        payoff_shape="vertical_bounded_directional",
        failure_modes=("adverse_move_breaches_short_strike", "wrong_direction", "iv_expansion"),
        rationale=(
            "V4.2 RE-AUDIT CORRECTION: V4.1 classified this as 'small_move_pinning' -- the "
            "same value used for a butterfly. Wrong: a credit spread is a ONE-SIDED "
            "THRESHOLD, not a center-seeking peak. It profits identically whether the "
            "underlying finishes just above the short put strike or far above it -- it never "
            "needs to stay NEAR any point, only to avoid crossing ONE boundary. Now correctly "
            "'directional_threshold': a bullish/non-bearish bounded-directional bet, typically "
            "short-vol/theta-positive, that profits as long as the downside does not exceed "
            "its threshold -- not a bet on finishing close to one center strike."
        ),
    ),
    StrategyCategory.CALL_CREDIT_SPREAD: StrategySemantics(
        category=StrategyCategory.CALL_CREDIT_SPREAD,
        directional_intent="bearish",
        move_intent="directional_threshold",
        volatility_intent="short_realized_move",
        payoff_shape="vertical_bounded_directional",
        failure_modes=("adverse_move_breaches_short_strike", "wrong_direction", "iv_expansion"),
        rationale="Mirror of PUT_CREDIT_SPREAD above the strike -- same V4.2 correction: a "
        "bearish/non-bullish directional threshold, not a pinning structure. Profits from "
        "the underlying staying below the short call strike, any distance below is equally "
        "fine.",
    ),
    StrategyCategory.LONG_STRADDLE: StrategySemantics(
        category=StrategyCategory.LONG_STRADDLE,
        directional_intent="direction_agnostic",
        move_intent="large_move",
        volatility_intent="long_realized_move",
        payoff_shape="two_sided_convex",
        failure_modes=("move_too_small_pinned_near_strike", "iv_crush", "spread_friction"),
        rationale="Genuinely long realized move in either direction -- both legs are long, "
        "no leg caps the other's exposure, and larger moves (either way) always help.",
    ),
    StrategyCategory.LONG_STRANGLE: StrategySemantics(
        category=StrategyCategory.LONG_STRANGLE,
        directional_intent="direction_agnostic",
        move_intent="large_move",
        volatility_intent="long_realized_move",
        payoff_shape="two_sided_convex",
        failure_modes=("move_too_small_pinned_in_range", "iv_crush", "spread_friction"),
        rationale="Same class as LONG_STRADDLE -- wider breakevens (OTM legs on both sides) "
        "mean it needs an even larger move to profit, but the underlying economics are "
        "identical: unambiguously long realized move.",
    ),
    StrategyCategory.IRON_CONDOR: StrategySemantics(
        category=StrategyCategory.IRON_CONDOR,
        directional_intent="neutral_range",
        move_intent="range_bound",
        volatility_intent="short_realized_move",
        payoff_shape="range_credit",
        failure_modes=("move_too_large_breaches_wing", "center_miss_near_short_strike"),
        rationale="A net-credit position that profits across a real width between two short "
        "strikes -- a genuinely wide, flat-topped profit plateau, not a single point. Hurt "
        "by a large move in either direction; the protective wings only bound the loss, "
        "they don't change the position's fundamentally short-realized-move economics. "
        "Distinct from a butterfly's sharp single peak (V4.1's DG evidence: even a modest "
        "+2.24% move that landed the exit near a short strike produced an outsized T+1 "
        "loss -- a real example of 'center miss near short strike', not a magnitude error).",
    ),
    StrategyCategory.LONG_CALL_BUTTERFLY: StrategySemantics(
        category=StrategyCategory.LONG_CALL_BUTTERFLY,
        directional_intent="neutral_range",
        move_intent="small_move_pinning",
        volatility_intent="short_realized_move",
        payoff_shape="tent_pinning",
        failure_modes=("move_too_large_either_direction", "spread_friction_on_doubled_short_leg"),
        rationale=(
            "THE CORE CORRECTION THIS REGISTRY EXISTS FOR: a 1-2-1 call butterfly is a "
            "net DEBIT (V3's _volatility_fit scores it LONG_VOL for exactly that reason), "
            "but its real payoff is a narrow tent peaking at the center strike and decaying "
            "to the full loss of the debit on either side -- it needs the underlying to stay "
            "NEAR the center, the same economic bet as a short-vol credit structure. Directional "
            "intent is 'center-dependent' at construction time (the center strike can sit above "
            "or below spot) but the exposure shape at any given center is a pure range/pinning "
            "bet, classified here as neutral_range for that reason. Confirmed against real "
            "settled data: CRM/VEEV/NVDA were all real LONG_VOL-labeled butterflies that lost "
            "precisely because the underlying moved MORE than this narrow tent could survive."
        ),
    ),
    StrategyCategory.IRON_BUTTERFLY: StrategySemantics(
        category=StrategyCategory.IRON_BUTTERFLY,
        directional_intent="neutral_range",
        move_intent="small_move_pinning",
        volatility_intent="short_realized_move",
        payoff_shape="tent_pinning",
        failure_modes=("move_too_large_either_direction", "spread_friction_on_shared_center"),
        rationale="Same tent-shaped, pinning economics as LONG_CALL_BUTTERFLY, constructed "
        "from a shared-strike straddle sale plus protective wings instead of an all-call "
        "1-2-1 -- net credit here, so it never had V3's debit-sign mislabeling problem, but "
        "the real payoff shape is the same sharp, single-peak tent (narrower than an iron "
        "condor's flatter, two-strike-wide plateau).",
    ),
}


def get_strategy_semantics(category: StrategyCategory) -> StrategySemantics:
    """The one authoritative lookup every future V4 consumer should use --
    never build a second, parallel debit/credit-based mapping elsewhere."""
    return _REGISTRY[category]


def all_strategy_semantics() -> dict[StrategyCategory, StrategySemantics]:
    """A defensive copy of the full registry, e.g. for a report or test
    that wants every family at once."""
    return dict(_REGISTRY)
