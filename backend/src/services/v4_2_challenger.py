"""V4.2 CHALLENGER evaluation and evidence freeze.

Not wired into any scheduler. Nothing in the production decision path calls
this; a caller must ask for it explicitly.

The shape this implements:

    ONE point-in-time evidence package
            |
            +-- V4.1 CONTROL      (already frozen, untouched)
            +-- V4.2 CHALLENGER   (frozen here, separately)

The challenger reads the control's OWN frozen candidate rows for its
economics. That is deliberate and is what keeps the comparison meaningful:
both methodologies see identical modeled T+1 valuations, identical strikes and
identical observed spreads, so any difference in outcome is attributable to
the gate and the ranking rather than to one of them having seen better market
data. It also means the challenger issues NO additional market-data request
for an event the control already evaluated -- the reuse is total, and is
reported as such on the decision row.

Failure isolation (Section 42) uses a SAVEPOINT, not a session rollback, for
the reason services/v4_shadow.py already documents: a bare rollback would
unwind the caller's entire transaction, so a challenger bug could discard
unrelated control work. That is precisely the coupling a challenger must not
introduce.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from analytics.decision.v4_2_earnings_friction import EARNINGS_FRICTION_VERSION
from analytics.decision.v4_2_expiry_ladder import EXPIRY_LADDER_VERSION
from analytics.decision.v4_2_viability import (
    DEFAULT_POLICY,
    MOVE_EDGE_VERSION,
    VIABILITY_GATE_VERSION,
    CandidateEconomics,
    ConfigurationConstraints,
    MoveEvidence,
    ViabilityPolicy,
    assess_viability,
    choose_v4_2_candidate,
    choose_v4_2_candidate_for_configuration,
    evaluate_move_edge,
)
from analytics.decision.v4_configurations import V4_CONFIGURATIONS
from analytics.earnings.v4_2_move_distribution import MOVE_DISTRIBUTION_VERSION
from analytics.earnings.v4_2_reaction_anchoring import REACTION_ANCHORING_VERSION
from models.v4_2_challenger import (
    CHALLENGER_SCHEMA_VERSION,
    V42ChallengerCandidate,
    V42ChallengerConfigResult,
    V42ChallengerDecision,
)
from models.v4_shadow import V4ShadowCandidate, V4ShadowCandidateLeg, V4ShadowDecision
from services.v4_2_move_history import anchored_move_distribution_for_ticker

CHALLENGER_STATUS_RANKED = "RANKED"
CHALLENGER_STATUS_NO_ACTION = "NO_ACTION"
CHALLENGER_STATUS_FAILED = "FAILED"
CHALLENGER_STATUS_ALREADY_FROZEN = "ALREADY_FROZEN"


@dataclass
class ChallengerEvaluation:
    """The result of evaluating one event, before (or without) persistence."""

    ticker: str
    status: str
    selected_candidate_id: str | None = None
    no_action_reason: str | None = None
    candidates_evaluated: int = 0
    candidates_accepted: int = 0
    decision_id: int | None = None
    latency_ms: Decimal | None = None
    unique_contracts_reused: int = 0
    market_data_requests_issued: int = 0
    candidate_rows: list[dict] = field(default_factory=list)
    config_rows: list[dict] = field(default_factory=list)
    move_context: Any = None
    implied_move_pct: Decimal | None = None
    failure_category: str | None = None
    failure_detail: str | None = None


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _reused_contract_count(db: Session, candidate_ids: list[int]) -> int:
    """Distinct contracts the challenger reasoned about, every one of them
    already observed by the control. This is the number that would otherwise
    have become new market-data subscriptions."""
    if not candidate_ids:
        return 0
    rows = (
        db.query(V4ShadowCandidateLeg.external_contract_id)
        .filter(V4ShadowCandidateLeg.shadow_candidate_id.in_(candidate_ids))
        .distinct()
        .all()
    )
    return len({r[0] for r in rows if r[0]})


def _economics_from_control(row: V4ShadowCandidate) -> CandidateEconomics:
    """The challenger's view of a candidate is the CONTROL's own frozen
    valuation. No revaluation, so no chance of the two diverging because one
    of them re-priced."""
    return CandidateEconomics(
        candidate_id=row.candidate_id,
        strategy=row.strategy,
        median_return=_decimal(row.core_median_return),
        worst_return=_decimal(row.core_worst_return),
        best_return=_decimal(row.core_best_return),
        positive_scenario_fraction=_decimal(row.core_positive_scenario_fraction),
        no_profitable_region=row.no_profitable_region,
        semantic_compatibility=_decimal(row.semantic_compatibility),
        mean_relative_spread=_decimal(row.mean_relative_spread),
    )


def evaluate_challenger(
    db: Session,
    decision: V4ShadowDecision,
    *,
    policy: ViabilityPolicy | None = None,
    settlement_date: date | None = None,
) -> ChallengerEvaluation:
    """Evaluate one event. Reads only; writes nothing."""
    started = time.monotonic()
    policy = policy or DEFAULT_POLICY

    rows = (
        db.query(V4ShadowCandidate)
        .filter_by(shadow_decision_id=decision.id, validity_status="RANKABLE")
        .all()
    )
    economics = [_economics_from_control(r) for r in rows]
    by_id = {r.candidate_id: r for r in rows}

    move = decision.expected_move or {}
    implied = _decimal(move.get("implied_move_pct")) if isinstance(move, dict) else None
    distribution = anchored_move_distribution_for_ticker(
        db, ticker=decision.ticker, as_of=decision.generated_at.date()
    )
    evidence = MoveEvidence(implied_move_pct=implied, distribution=distribution)

    outcome = choose_v4_2_candidate(economics, evidence, policy)

    candidate_rows: list[dict] = []
    ranked = sorted(
        (v for v in outcome.verdicts if v.acceptable),
        key=lambda v: (by_id[v.candidate_id].core_median_return or Decimal(0)),
        reverse=True,
    )
    rank_by_id = {v.candidate_id: i + 1 for i, v in enumerate(ranked)}

    for verdict in outcome.verdicts:
        control = by_id[verdict.candidate_id]
        edge = evaluate_move_edge(control.strategy, evidence, policy)
        candidate_rows.append(
            {
                "candidate_id": control.candidate_id,
                "strategy": control.strategy,
                "expiration": control.expiration,
                # The control froze exactly one expiry, so every candidate
                # sits on ladder rung 0. A future multi-expiry run records
                # the real rung; nothing is invented here.
                "expiry_ladder_position": 0,
                "entry_dte": (control.expiration - decision.generated_at.date()).days,
                "dte_at_settlement": (
                    (control.expiration - settlement_date).days if settlement_date else None
                ),
                "geometry_variant_id": control.geometry_variant_id,
                "semantic_compatibility": control.semantic_compatibility,
                "semantic_tier": control.semantic_tier,
                "core_median_return": control.core_median_return,
                "core_worst_return": control.core_worst_return,
                "core_best_return": control.core_best_return,
                "core_positive_scenario_fraction": control.core_positive_scenario_fraction,
                "no_profitable_region": control.no_profitable_region,
                "move_edge_status": edge.status,
                "move_edge_exposure": edge.exposure,
                "move_edge_ratio": edge.edge_ratio,
                "move_edge_threshold": edge.threshold,
                "move_edge_explanation": edge.explanation,
                "mean_relative_spread": control.mean_relative_spread,
                "worst_relative_spread": control.worst_relative_spread,
                "capital_utilisation": control.capital_utilisation,
                "entry_cash_required": control.entry_cash_required,
                "viability_acceptable": verdict.acceptable,
                "viability_reason_codes": list(verdict.reason_codes),
                "viability_detail": list(verdict.detail),
                "rank": rank_by_id.get(verdict.candidate_id),
                "market_data_quality": control.market_data_quality,
            }
        )

    config_rows: list[dict] = []
    for config in V4_CONFIGURATIONS:
        constraints = ConfigurationConstraints(
            key=config.key,
            capital_base=config.capital_base,
            max_risk_dollars=config.max_risk_dollars,
        )
        per_config = choose_v4_2_candidate_for_configuration(
            economics,
            evidence,
            constraints,
            entry_cash_by_candidate={
                r.candidate_id: Decimal(str(r.entry_cash_required))
                for r in rows
                if r.entry_cash_required is not None
            },
            policy=policy,
        )
        config_rows.append(
            {
                "configuration_key": config.key,
                "capital_base": config.capital_base,
                "risk_profile": config.risk_profile.value,
                "max_risk_dollars": config.max_risk_dollars,
                "status": per_config.status,
                "selected_candidate_id": per_config.selected_candidate_id,
                "no_action_reason": per_config.no_action_reason,
            }
        )

    return ChallengerEvaluation(
        ticker=decision.ticker,
        status=outcome.status,
        selected_candidate_id=outcome.selected_candidate_id,
        no_action_reason=outcome.no_action_reason,
        candidates_evaluated=len(economics),
        candidates_accepted=len(outcome.accepted),
        latency_ms=Decimal(str((time.monotonic() - started) * 1000)),
        # Total reuse: the challenger re-priced nothing and subscribed to
        # nothing, so every contract it reasoned about came from the
        # control's own frozen observation. Counted from the real leg rows.
        unique_contracts_reused=_reused_contract_count(db, [r.id for r in rows]),
        market_data_requests_issued=0,
        candidate_rows=candidate_rows,
        config_rows=config_rows,
        move_context=distribution,
        implied_move_pct=implied,
    )


def freeze_challenger_decision(
    db: Session,
    decision: V4ShadowDecision,
    evaluation: ChallengerEvaluation,
    *,
    observed_at: datetime | None = None,
) -> ChallengerEvaluation:
    """Persist one challenger decision, its complete candidate set and its
    six configuration outcomes.

    Idempotent (Section 41) and savepoint-isolated (Section 42). Never raises
    at this boundary: a challenger failure becomes recorded challenger
    evidence and the control's transaction is left intact.
    """
    observed_at = observed_at or decision.generated_at
    existing = (
        db.query(V42ChallengerDecision)
        .filter_by(
            earnings_calendar_event_id=decision.earnings_calendar_event_id,
            gate_version=VIABILITY_GATE_VERSION,
            observed_at=observed_at,
        )
        .one_or_none()
    )
    if existing is not None:
        evaluation.status = CHALLENGER_STATUS_ALREADY_FROZEN
        evaluation.decision_id = existing.id
        return evaluation

    savepoint = db.begin_nested()
    try:
        context = evaluation.move_context
        row = V42ChallengerDecision(
            earnings_calendar_event_id=decision.earnings_calendar_event_id,
            shadow_decision_id=decision.id,
            ticker=decision.ticker,
            generated_at=datetime.now(UTC),
            observed_at=observed_at,
            schema_version=CHALLENGER_SCHEMA_VERSION,
            gate_version=VIABILITY_GATE_VERSION,
            move_edge_version=MOVE_EDGE_VERSION,
            move_distribution_version=MOVE_DISTRIBUTION_VERSION,
            reaction_anchoring_version=REACTION_ANCHORING_VERSION,
            expiry_ladder_version=EXPIRY_LADDER_VERSION,
            friction_version=EARNINGS_FRICTION_VERSION,
            ranking_version=f"{VIABILITY_GATE_VERSION}:economics_first",
            decision_view_schema_version=decision.decision_view_schema_version,
            historical_sample_n=getattr(context, "sample_n", None),
            historical_evidence_quality=getattr(context, "quality", None),
            historical_timing_quality=getattr(context, "timing_quality", None),
            historical_median_abs_move_pct=getattr(context, "median_abs_move_pct", None),
            historical_p25_abs_move_pct=getattr(context, "p25_abs_move_pct", None),
            historical_p75_abs_move_pct=getattr(context, "p75_abs_move_pct", None),
            historical_source_digest=getattr(context, "source_digest", None),
            historical_source_event_count=getattr(context, "source_event_count", None),
            historical_as_of=getattr(context, "as_of", None),
            implied_move_pct=evaluation.implied_move_pct,
            underlying_price=decision.underlying_price,
            market_data_quality=decision.market_data_quality,
            status=evaluation.status,
            selected_candidate_id=evaluation.selected_candidate_id,
            no_action_reason=evaluation.no_action_reason,
            candidates_evaluated=evaluation.candidates_evaluated,
            candidates_accepted=evaluation.candidates_accepted,
            total_latency_ms=evaluation.latency_ms,
            metadata_request_count=0,
            contract_detail_request_count=0,
            market_data_request_count=evaluation.market_data_requests_issued,
            unique_contracts_quoted=0,
            reused_control_contracts=evaluation.unique_contracts_reused,
        )
        db.add(row)
        db.flush()

        for candidate in evaluation.candidate_rows:
            db.add(V42ChallengerCandidate(challenger_decision_id=row.id, **candidate))
        for config in evaluation.config_rows:
            db.add(V42ChallengerConfigResult(challenger_decision_id=row.id, **config))
        db.flush()
        savepoint.commit()
        evaluation.decision_id = row.id
        return evaluation
    except IntegrityError as exc:
        # The unique constraint is the real idempotency guarantee; a race
        # that loses it is a no-op, not a failure.
        savepoint.rollback()
        evaluation.status = CHALLENGER_STATUS_ALREADY_FROZEN
        evaluation.failure_detail = f"{type(exc).__name__}"
        return evaluation
    except Exception as exc:  # noqa: BLE001 -- a challenger fault must never reach the control
        savepoint.rollback()
        evaluation.status = CHALLENGER_STATUS_FAILED
        evaluation.failure_category = type(exc).__name__
        evaluation.failure_detail = str(exc)
        return evaluation


def evaluate_and_freeze(
    db: Session,
    decision: V4ShadowDecision,
    *,
    policy: ViabilityPolicy | None = None,
    settlement_date: date | None = None,
    dry_run: bool = True,
) -> ChallengerEvaluation:
    """Evaluate, and persist only when explicitly asked to.

    ``dry_run`` defaults to True so that merely importing and calling this in
    an exploratory context cannot write forward evidence.
    """
    try:
        evaluation = evaluate_challenger(
            db, decision, policy=policy, settlement_date=settlement_date
        )
    except Exception as exc:  # noqa: BLE001
        # The handler must not itself depend on the input being well formed:
        # isolation that only works when the caller behaved is not isolation.
        return ChallengerEvaluation(
            ticker=str(getattr(decision, "ticker", "unknown")),
            status=CHALLENGER_STATUS_FAILED,
            failure_category=type(exc).__name__,
            failure_detail=str(exc),
        )
    if dry_run:
        return evaluation
    return freeze_challenger_decision(db, decision, evaluation)


__all__ = [
    "CHALLENGER_STATUS_ALREADY_FROZEN",
    "CHALLENGER_STATUS_FAILED",
    "CHALLENGER_STATUS_NO_ACTION",
    "CHALLENGER_STATUS_RANKED",
    "ChallengerEvaluation",
    "assess_viability",
    "evaluate_and_freeze",
    "evaluate_challenger",
    "freeze_challenger_decision",
]
