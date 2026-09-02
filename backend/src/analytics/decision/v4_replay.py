"""Options Decision Engine V4.2 -- Real V3 Decision Replay (2026-09-01).

Replays every real, already-frozen V3 DecisionSnapshot through V4.2
semantic compatibility, using ONLY decision-time information (direction,
volatility_view, selected strategy) -- never realized stock move,
realized P&L, settlement outcome, or post-event IV (this task's own
explicit Section 19 anti-lookahead rule). This module never queries
realized-outcome tables and its return type carries no such field, so
that rule cannot be violated by construction, not merely by convention.

NOT a counterfactual P&L simulation. It answers exactly one question per
decision: "would V4.2 semantics consider the V3-selected strategy
coherent with the AI's own stated view, using only what was knowable at
decision time?" Real settled outcomes may only be compared AFTERWARD,
by a human or a report, never fed back into this module.

Pure function over already-known inputs -- no DB session, no live call.
The real 23-row V3 dataset used to produce this task's report was
queried once, read-only, directly from the production database (see
the V4.2 final report), and is NOT hardcoded here as a fixture module --
this module is the general-purpose replay function any real or
synthetic DecisionSnapshot-shaped input can be run through.
"""

from dataclasses import dataclass

from analytics.decision.v4_compatibility import (
    SemanticCompatibilityResult,
    evaluate_semantic_compatibility,
)
from analytics.decision.v4_market_view import derive_v4_market_view
from analytics.decision.v4_strategy_semantics import get_strategy_semantics
from analytics.options.strategy_candidates import StrategyCategory
from models.enums import DecisionDirection, DecisionVolatilityView


@dataclass(frozen=True)
class V3DecisionReplayInput:
    """Decision-time-only fields from one real (or synthetic, for
    testing) DecisionSnapshot -- deliberately excludes every settlement/
    outcome field that exists on the real row."""

    ticker: str
    direction: DecisionDirection
    volatility_view: DecisionVolatilityView | None
    strategy_type: str | None  # None/empty for a genuine NO_ACTION decision


@dataclass(frozen=True)
class V3DecisionReplayResult:
    ticker: str
    v3_direction: str
    v3_volatility_view: str | None
    v3_selected_strategy: str | None
    compatibility: SemanticCompatibilityResult | None
    skip_reason: str | None


def replay_v3_decision(decision: V3DecisionReplayInput) -> V3DecisionReplayResult:
    if not decision.strategy_type:
        return V3DecisionReplayResult(
            ticker=decision.ticker,
            v3_direction=decision.direction.value,
            v3_volatility_view=decision.volatility_view.value if decision.volatility_view else None,
            v3_selected_strategy=None,
            compatibility=None,
            skip_reason="NO_ACTION -- no strategy was selected, nothing to evaluate.",
        )

    try:
        category = StrategyCategory(decision.strategy_type)
    except ValueError:
        return V3DecisionReplayResult(
            ticker=decision.ticker,
            v3_direction=decision.direction.value,
            v3_volatility_view=decision.volatility_view.value if decision.volatility_view else None,
            v3_selected_strategy=decision.strategy_type,
            compatibility=None,
            skip_reason=f"Unrecognized strategy_type {decision.strategy_type!r}.",
        )

    market_view = derive_v4_market_view(decision.direction, decision.volatility_view)
    semantics = get_strategy_semantics(category)
    compatibility = evaluate_semantic_compatibility(market_view, semantics)

    return V3DecisionReplayResult(
        ticker=decision.ticker,
        v3_direction=decision.direction.value,
        v3_volatility_view=decision.volatility_view.value if decision.volatility_view else None,
        v3_selected_strategy=decision.strategy_type,
        compatibility=compatibility,
        skip_reason=None,
    )


def replay_many(decisions: list[V3DecisionReplayInput]) -> list[V3DecisionReplayResult]:
    return [replay_v3_decision(d) for d in decisions]
