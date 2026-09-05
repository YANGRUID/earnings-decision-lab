"""Replay the V4.2 challenger gate over frozen V4.1 evidence.

Strictly ex-ante: every input is a value that was already persisted on
``v4_shadow_candidate`` at the moment the real decision was made -- modeled
median / worst / best returns, positive-scenario fraction, semantic
compatibility, observed entry spread -- plus the event's own implied move
from ``v4_shadow_decision.expected_move``.

No realized outcome is read here, and none is needed: the gate is an
ex-ante economic judgement. Realized results are joined only afterwards, by
the report, to classify what happened -- never to choose a threshold.

This writes nothing. It is a read-only comparison of two methodologies over
identical frozen evidence.
"""

import json
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.decision.v4_2_viability import (
    DEFAULT_POLICY,
    CandidateEconomics,
    ChallengerDecision,
    MoveEvidence,
    ViabilityPolicy,
    choose_v4_2_candidate,
)
from models.v4_shadow import V4ShadowCandidate, V4ShadowDecision
from services.v4_2_move_history import move_distribution_for_ticker


@dataclass
class EventReplay:
    ticker: str
    decision_id: int
    v41_candidate_id: str | None
    v41_strategy: str | None
    v41_median: Decimal | None
    v42: ChallengerDecision
    implied_move_pct: Decimal | None
    historical_sample_n: int
    historical_quality: str | None
    historical_median_abs_move_pct: Decimal | None
    edge_ratio: Decimal | None
    candidates_considered: int

    @property
    def changed(self) -> bool:
        return self.v42.selected_candidate_id != self.v41_candidate_id


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def replay_event(
    db: Session, decision: V4ShadowDecision, policy: ViabilityPolicy = DEFAULT_POLICY
) -> EventReplay:
    rows = (
        db.query(V4ShadowCandidate)
        .filter_by(shadow_decision_id=decision.id, validity_status="RANKABLE")
        .all()
    )
    economics = [
        CandidateEconomics(
            candidate_id=r.candidate_id,
            strategy=r.strategy,
            median_return=_decimal(r.core_median_return),
            worst_return=_decimal(r.core_worst_return),
            best_return=_decimal(r.core_best_return),
            positive_scenario_fraction=_decimal(r.core_positive_scenario_fraction),
            no_profitable_region=r.no_profitable_region,
            semantic_compatibility=_decimal(r.semantic_compatibility),
            mean_relative_spread=_decimal(r.mean_relative_spread),
        )
        for r in rows
    ]

    move = decision.expected_move or {}
    if isinstance(move, str):
        move = json.loads(move)
    # The implied move is the one number V4.1 already computed correctly and
    # froze on the decision -- reused verbatim, never recomputed.
    implied = _decimal(move.get("implied_move_pct"))
    # The historical distribution is rebuilt POINT-IN-TIME from the decision's
    # own date. V4.1 recorded historical_sample_n = 0 on every event because
    # the production path never passed historical moves in at all; that gap is
    # reported separately and is not fixed in the V4.1 path here.
    distribution = move_distribution_for_ticker(
        db, ticker=decision.ticker, as_of=decision.generated_at.date()
    )
    evidence = MoveEvidence(implied_move_pct=implied, distribution=distribution)

    selected = next((r for r in rows if r.rank == 1), None)
    return EventReplay(
        ticker=decision.ticker,
        decision_id=decision.id,
        v41_candidate_id=decision.rank_1_candidate_id,
        v41_strategy=selected.strategy if selected else None,
        v41_median=_decimal(selected.core_median_return) if selected else None,
        v42=choose_v4_2_candidate(economics, evidence, policy),
        implied_move_pct=implied,
        historical_sample_n=distribution.sample_n,
        historical_quality=distribution.quality,
        historical_median_abs_move_pct=distribution.median_abs_move_pct,
        edge_ratio=evidence.edge_ratio,
        candidates_considered=len(economics),
    )


def replay_all(
    db: Session, policy: ViabilityPolicy = DEFAULT_POLICY
) -> list[EventReplay]:
    decisions = db.query(V4ShadowDecision).order_by(V4ShadowDecision.id).all()
    return [replay_event(db, d, policy) for d in decisions]
