"""Options Decision Engine V4.2 -- View <-> Strategy Semantic
Compatibility Engine (2026-09-01).

Answers ONE question, deterministically, from options-payoff economics
alone: HOW COMPATIBLE IS THIS STRATEGY'S REAL PAYOFF SHAPE WITH THE
STATED MARKET VIEW? Never "how good is the trade overall" (V4.4's job,
not built here) and never fitted against the 7 real settled trades'
realized outcomes (this task's own explicit Section 19/27 anti-lookahead
rule -- see test_v4_compatibility_replay.py for how those trades are
used instead: as read-only validation examples, never as curve-fitting
input).

V3 DOES NOT DEPEND ON THIS MODULE. analytics/decision/strategy_scoring.py
is untouched; this engine is reachable only through tests, the
diagnostic replay module, and a clearly-experimental read-only API route
-- see this module's own "V4.2 remains inert" note at the bottom.

DESIGN: four independent components (direction, move magnitude,
volatility, payoff shape), each scored on a 5-point scale via one
centralized, auditable matrix -- never scattered if/else. The overall
score is the MINIMUM of the four, not an average: this is the direct,
deliberate fix for the exact mistake this whole project is correcting
(Section 11 of this task) -- no single strong dimension may mask a
contradiction in another. A long call scoring perfectly on convexity
must not average up to "good" if its direction is wrong; a butterfly
scoring perfectly on direction must not average up to "good" if its
move/volatility expectation is a contradiction.
"""

from dataclasses import dataclass

from analytics.decision.v4_market_view import ExpectedMoveIntent, V4MarketView
from analytics.decision.v4_strategy_semantics import (
    DirectionalIntent,
    MoveMagnitudeIntent,
    PayoffShape,
    StrategySemantics,
    VolatilityIntent,
)
from models.enums import DecisionDirection, DecisionVolatilityView

COMPATIBILITY_VERSION = "view_strategy_compatibility_v1"

# The five-point scale every component score is drawn from -- named so a
# human-readable tier can be attached to any score without re-deriving
# thresholds ad hoc.
CONTRADICTION = 0.0
POOR = 0.25
CONDITIONAL = 0.5
GOOD = 0.75
STRONG = 1.0


def compatibility_tier(score: float) -> str:
    if score <= CONTRADICTION:
        return "contradiction"
    if score <= POOR:
        return "poor"
    if score <= CONDITIONAL:
        return "conditional"
    if score <= GOOD:
        return "good"
    return "strong"


# --------------------------------------------------------------------------
# Reason codes -- explicit, named, never a bare number with no explanation
# (this task's own Section 8).
# --------------------------------------------------------------------------

DIRECTION_STRONG_FIT = "DIRECTION_STRONG_FIT"
DIRECTION_CONTRADICTION = "DIRECTION_CONTRADICTION"
MOVE_INTENT_STRONG_FIT = "MOVE_INTENT_STRONG_FIT"
MOVE_INTENT_CONTRADICTION = "MOVE_INTENT_CONTRADICTION"
VOLATILITY_STRONG_FIT = "VOLATILITY_STRONG_FIT"
VOLATILITY_CONTRADICTION = "VOLATILITY_CONTRADICTION"
PAYOFF_SHAPE_MISMATCH = "PAYOFF_SHAPE_MISMATCH"
MARKET_VIEW_UNDERSPECIFIED = "MARKET_VIEW_UNDERSPECIFIED"


# --------------------------------------------------------------------------
# DIRECTION MATRIX -- real DecisionDirection (5-way, the actual LLM
# output) x strategy DirectionalIntent (4-way, from the semantics
# registry). Centralized here, not scattered if/else, per this task's
# Section 10.
#
# Rationale for the shape of this matrix: a directionally agnostic
# strategy is always at least "good" (0.75) for any directional view,
# since it never contradicts a direction -- it just doesn't specifically
# capture the conviction the way a matching directional structure would.
# A neutral_range (pinning/range) strategy is only "good" (1.0) under a
# NEUTRAL view specifically; it is a real, if partial, contradiction
# under any directional (bullish/bearish, strong or not) view, since a
# genuine directional conviction argues against betting the stock stays
# put. Opposite-direction strategies (BULLISH view x bearish structure)
# are always a hard 0.0 contradiction -- this is the literal worked
# example this task's own Section 8 gives (BULLISH + BEAR_PUT_SPREAD ->
# DIRECTION_CONTRADICTION).
# --------------------------------------------------------------------------

_DIRECTION_MATRIX: dict[DecisionDirection, dict[DirectionalIntent, float]] = {
    DecisionDirection.STRONG_BULLISH: {
        "bullish": STRONG,
        "direction_agnostic": GOOD,
        "neutral_range": CONTRADICTION,
        "bearish": CONTRADICTION,
    },
    DecisionDirection.BULLISH: {
        "bullish": STRONG,
        "direction_agnostic": GOOD,
        "neutral_range": POOR,
        "bearish": CONTRADICTION,
    },
    DecisionDirection.NEUTRAL: {
        "bullish": POOR,
        "direction_agnostic": STRONG,
        "neutral_range": STRONG,
        "bearish": POOR,
    },
    DecisionDirection.BEARISH: {
        "bullish": CONTRADICTION,
        "direction_agnostic": GOOD,
        "neutral_range": POOR,
        "bearish": STRONG,
    },
    DecisionDirection.STRONG_BEARISH: {
        "bullish": CONTRADICTION,
        "direction_agnostic": GOOD,
        "neutral_range": CONTRADICTION,
        "bearish": STRONG,
    },
}


# --------------------------------------------------------------------------
# MOVE MATRIX -- market-view ExpectedMoveIntent x strategy
# MoveMagnitudeIntent. This is where the butterfly/credit-spread
# distinction (this task's Sections 2/12/14) actually bites: a butterfly
# ("small_move_pinning") and a credit spread ("directional_threshold")
# score differently against a large_move view specifically because a
# large FAVORABLE move doesn't hurt a threshold structure the way it
# devastates a pinning one.
# --------------------------------------------------------------------------

_MOVE_MATRIX: dict[ExpectedMoveIntent, dict[MoveMagnitudeIntent, float]] = {
    "large_move": {
        "large_move": STRONG,
        "moderate_move": GOOD,
        "small_move_pinning": CONTRADICTION,
        "range_bound": CONTRADICTION,
        "directional_threshold": CONDITIONAL,
    },
    "moderate_move": {
        "large_move": CONDITIONAL,
        "moderate_move": STRONG,
        "small_move_pinning": POOR,
        "range_bound": CONDITIONAL,
        "directional_threshold": GOOD,
    },
    "small_move": {
        "large_move": CONTRADICTION,
        "moderate_move": POOR,
        "small_move_pinning": STRONG,
        "range_bound": STRONG,
        "directional_threshold": STRONG,
    },
    "unspecified": {
        "large_move": CONDITIONAL,
        "moderate_move": CONDITIONAL,
        "small_move_pinning": CONDITIONAL,
        "range_bound": CONDITIONAL,
        "directional_threshold": CONDITIONAL,
    },
}


# --------------------------------------------------------------------------
# VOLATILITY MATRIX -- real DecisionVolatilityView (or None, for the
# genuine pre-Phase-4 rows that never recorded one) x strategy
# VolatilityIntent. mixed_path_dependent (debit verticals) is never a
# strong OR a contradiction against any view -- it is real, bounded
# vega, deliberately never collapsed to a binary long/short read.
# --------------------------------------------------------------------------

_VOLATILITY_MATRIX: dict[DecisionVolatilityView | None, dict[VolatilityIntent, float]] = {
    DecisionVolatilityView.LONG_VOL: {
        "long_realized_move": STRONG,
        "short_realized_move": CONTRADICTION,
        "mixed_path_dependent": GOOD,
    },
    DecisionVolatilityView.SHORT_VOL: {
        "long_realized_move": CONTRADICTION,
        "short_realized_move": STRONG,
        "mixed_path_dependent": GOOD,
    },
    DecisionVolatilityView.NEUTRAL_VOL: {
        "long_realized_move": CONDITIONAL,
        "short_realized_move": CONDITIONAL,
        "mixed_path_dependent": GOOD,
    },
    None: {
        "long_realized_move": CONDITIONAL,
        "short_realized_move": CONDITIONAL,
        "mixed_path_dependent": CONDITIONAL,
    },
}


# --------------------------------------------------------------------------
# PAYOFF SHAPE ARCHETYPES -- derived from (is the view directional?, is
# the expected move large?), the two facts that jointly determine what
# payoff topology a view "wants." A structural cross-check independent
# of the move/volatility matrices above, not a restatement of them.
# --------------------------------------------------------------------------

_PAYOFF_ARCHETYPE: dict[tuple[str, str], dict[PayoffShape, float]] = {
    ("directional", "large"): {
        "single_sided_convex": STRONG,
        "vertical_bounded_directional": GOOD,
        "two_sided_convex": CONDITIONAL,
        "tent_pinning": CONTRADICTION,
        "range_credit": CONTRADICTION,
    },
    ("directional", "not_large"): {
        "vertical_bounded_directional": STRONG,
        "single_sided_convex": GOOD,
        "two_sided_convex": CONTRADICTION,
        "tent_pinning": CONTRADICTION,
        "range_credit": CONTRADICTION,
    },
    ("neutral", "large"): {
        "two_sided_convex": STRONG,
        "single_sided_convex": CONDITIONAL,
        "vertical_bounded_directional": CONTRADICTION,
        "tent_pinning": CONTRADICTION,
        "range_credit": CONTRADICTION,
    },
    ("neutral", "not_large"): {
        "tent_pinning": STRONG,
        "range_credit": STRONG,
        "vertical_bounded_directional": POOR,
        "two_sided_convex": CONTRADICTION,
        "single_sided_convex": CONTRADICTION,
    },
}

_DIRECTIONAL_VIEWS = frozenset(
    {
        DecisionDirection.STRONG_BULLISH,
        DecisionDirection.BULLISH,
        DecisionDirection.BEARISH,
        DecisionDirection.STRONG_BEARISH,
    }
)


def _payoff_shape_compatibility(
    market_view: V4MarketView, payoff_shape: PayoffShape
) -> tuple[float, bool]:
    """Returns (score, underspecified). Underspecified when the view's
    own move_intent is "unspecified" -- the archetype lookup is skipped
    entirely rather than guessing a large/not_large bucket for it."""
    if market_view.expected_move_intent == "unspecified":
        return CONDITIONAL, True
    direction_class = "directional" if market_view.direction in _DIRECTIONAL_VIEWS else "neutral"
    move_class = "large" if market_view.expected_move_intent == "large_move" else "not_large"
    return _PAYOFF_ARCHETYPE[(direction_class, move_class)][payoff_shape], False


@dataclass(frozen=True)
class SemanticCompatibilityResult:
    """HOW COMPATIBLE, never HOW GOOD OVERALL -- see this module's own
    docstring. ``overall_semantic_compatibility`` is the minimum of the
    four components, deliberately, so a strong single dimension can
    never mask a contradiction elsewhere."""

    direction_compatibility: float
    move_magnitude_compatibility: float
    volatility_compatibility: float
    payoff_shape_compatibility: float
    overall_semantic_compatibility: float
    reason_codes: tuple[str, ...]
    explanation: str

    @property
    def tier(self) -> str:
        return compatibility_tier(self.overall_semantic_compatibility)


def evaluate_semantic_compatibility(
    market_view: V4MarketView, strategy_semantics: StrategySemantics
) -> SemanticCompatibilityResult:
    """Pure function: no DB access, no LLM call, no realized-outcome
    input of any kind (this task's own explicit anti-lookahead rule,
    Section 19) -- only the stated view and the strategy's own real
    payoff-geometry classification."""
    direction_score = _DIRECTION_MATRIX[market_view.direction][
        strategy_semantics.directional_intent
    ]
    move_score = _MOVE_MATRIX[market_view.expected_move_intent][strategy_semantics.move_intent]
    volatility_score = _VOLATILITY_MATRIX[market_view.volatility_view][
        strategy_semantics.volatility_intent
    ]
    payoff_score, payoff_underspecified = _payoff_shape_compatibility(
        market_view, strategy_semantics.payoff_shape
    )

    overall = min(direction_score, move_score, volatility_score, payoff_score)

    reason_codes: list[str] = []
    if direction_score <= CONTRADICTION:
        reason_codes.append(DIRECTION_CONTRADICTION)
    elif direction_score >= STRONG:
        reason_codes.append(DIRECTION_STRONG_FIT)
    if move_score <= CONTRADICTION:
        reason_codes.append(MOVE_INTENT_CONTRADICTION)
    elif move_score >= STRONG:
        reason_codes.append(MOVE_INTENT_STRONG_FIT)
    if volatility_score <= CONTRADICTION:
        reason_codes.append(VOLATILITY_CONTRADICTION)
    elif volatility_score >= STRONG:
        reason_codes.append(VOLATILITY_STRONG_FIT)
    if payoff_score <= CONTRADICTION:
        reason_codes.append(PAYOFF_SHAPE_MISMATCH)
    if (
        market_view.expected_move_intent == "unspecified"
        or market_view.volatility_view is None
        or payoff_underspecified
    ):
        reason_codes.append(MARKET_VIEW_UNDERSPECIFIED)

    explanation = _build_explanation(
        market_view, strategy_semantics, direction_score, move_score, volatility_score, overall
    )

    return SemanticCompatibilityResult(
        direction_compatibility=direction_score,
        move_magnitude_compatibility=move_score,
        volatility_compatibility=volatility_score,
        payoff_shape_compatibility=payoff_score,
        overall_semantic_compatibility=overall,
        reason_codes=tuple(reason_codes),
        explanation=explanation,
    )


def _build_explanation(
    market_view: V4MarketView,
    strategy_semantics: StrategySemantics,
    direction_score: float,
    move_score: float,
    volatility_score: float,
    overall: float,
) -> str:
    vol_view_label = (
        market_view.volatility_view.value if market_view.volatility_view else "none recorded"
    )
    parts = [
        f"{market_view.direction.value} view vs. {strategy_semantics.directional_intent} "
        f"structure: {compatibility_tier(direction_score)}.",
        f"Expected move '{market_view.expected_move_intent}' vs. structure's "
        f"'{strategy_semantics.move_intent}': {compatibility_tier(move_score)}.",
        f"Volatility view '{vol_view_label}' vs. structure's "
        f"'{strategy_semantics.volatility_intent}': {compatibility_tier(volatility_score)}.",
        f"Overall: {compatibility_tier(overall)} "
        f"(weakest of the four components, never averaged up).",
    ]
    if overall <= CONTRADICTION:
        worst = min(
            ("direction", direction_score),
            ("move magnitude", move_score),
            ("volatility", volatility_score),
            key=lambda item: item[1],
        )
        parts.append(
            f"Real contradiction on {worst[0]} -- a plausible failure mode here: "
            f"{strategy_semantics.failure_modes[0] if strategy_semantics.failure_modes else 'n/a'}."
        )
    return " ".join(parts)


# V4.2 REMAINS INERT: this module is imported only by
# analytics/decision/v4_replay.py (a read-only diagnostic),
# api/routers/v4_experimental.py (a clearly-experimental, non-official
# read-only route, never registered as part of any real trading
# workflow), and this module's own tests. No import from
# services/scheduler.py, services/decision_pipeline.py,
# services/decision_engine.py, or services/benchmark_entry_capture.py
# exists -- verified by an exhaustive grep, see
# tests/test_v4_2_v3_isolation.py.
