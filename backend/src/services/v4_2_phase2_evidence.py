"""V4.2 PHASE 2 -- freezing independent-search evidence.

Phase 2 writes into the SAME challenger tables as Phase 1, discriminated by
``methodology_version``. Two sets of tables holding the same kind of evidence
would mean two entry paths, two settlement paths and two track records, and
the first time one of them was fixed the other would drift. Additive columns
and one discriminator keep a single pipeline.

THE DISCRIMINATOR, AND THE NULL THAT MEANS PHASE 1
--------------------------------------------------
Rows written before Phase 2 existed carry NULL, and are not backfilled:
stamping a claim into frozen evidence that the evidence never made is the
thing a forward test must never do. So NULL means Phase 1, permanently, for
rows past and future -- Phase 1 keeps writing NULL, and both phases filter on
the discriminator so neither can mistake the other's row for its own. Without
that, Phase 1's idempotency lookup (event, gate version, observed instant)
would match a Phase-2 row and silently report ALREADY_FROZEN, suppressing the
control's own challenger.

THE STATUS VOCABULARY
---------------------
The engine speaks ACTION/NO_ACTION, as the phase-2 brief does. The column has
meant RANKED/NO_ACTION since Phase 1, and the entry service filters on
RANKED. Two vocabularies in one column is how a config result silently stops
producing entries, so the mapping happens here, once, at the persistence
boundary.

WHAT IS PERSISTED ONCE, AND WHAT PER CONFIGURATION
--------------------------------------------------
Shared event evidence -- the move distribution, the underlying observation,
the request budget, the ladder -- is written once on the decision row. The
complete bounded candidate universe is written once, refused candidates
included, because a reader who cannot see what was declined cannot tell a
thin field from a strong one. Only the SELECTION is per configuration, and
each configuration's row carries its own rank, quantity, capital used, max
risk used and stage-by-stage rejection census.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from analytics.decision.v4_2_config_policy import STATUS_ACTION
from analytics.decision.v4_2_phase2_methodology import PHASE_2_METHODOLOGY, PHASE_2_VERSIONS
from analytics.decision.v4_2_viability import (
    DEFAULT_POLICY,
    MoveEvidence,
    ViabilityPolicy,
    evaluate_move_edge,
)
from analytics.earnings.v4_2_move_distribution import MOVE_DISTRIBUTION_VERSION
from analytics.earnings.v4_2_reaction_anchoring import REACTION_ANCHORING_VERSION
from models.v4_2_challenger import (
    CHALLENGER_SCHEMA_VERSION,
    V42ChallengerCandidate,
    V42ChallengerConfigResult,
    V42ChallengerDecision,
)
from models.v4_shadow import V4ShadowDecision
from services.v4_2_phase2 import Phase2Evaluation

log = logging.getLogger("services.v4_2_phase2_evidence")

FREEZE_FROZEN = "FROZEN"
FREEZE_ALREADY_FROZEN = "ALREADY_FROZEN"
FREEZE_FAILED = "FAILED"
FREEZE_NOT_ACTIVATED = "NOT_ACTIVATED"

#: The persisted word for a configuration that chose something. See the
#: module docstring: the engine says ACTION, this column has said RANKED
#: since Phase 1, and the entry service filters on RANKED.
_PERSISTED_ACTION = "RANKED"

#: A per-configuration rejection list is unbounded in principle (one entry per
#: candidate per configuration). Capped so a pathological universe cannot turn
#: one decision into a multi-megabyte row; the census counts are complete
#: either way, and the cap is recorded when it bites.
_MAX_PERSISTED_REJECTIONS = 40


class Phase2FreezeResult:
    def __init__(self, status: str, decision_id: int | None = None, detail: str | None = None):
        self.status = status
        self.decision_id = decision_id
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover -- diagnostics only
        return f"Phase2FreezeResult({self.status!r}, {self.decision_id!r}, {self.detail!r})"


def phase2_activated(
    *, enabled: bool, activation_at: datetime | None, legal_decision_window_at: datetime
) -> tuple[bool, str]:
    """Whether this event may create Phase-2 FORWARD evidence.

    Two independent conditions, both required. The flag is the operator's
    switch; the activation instant is the absolute prospective floor that
    makes "no historical backfill" auditable rather than merely likely. An
    unset activation instant means the phase has never been activated, and no
    event qualifies -- deliberately, so that turning the flag on by itself
    cannot retroactively produce rows for events already past.
    """
    if not enabled:
        return False, "Phase 2 is not enabled"
    if activation_at is None:
        return False, "Phase 2 has no activation instant, so no event is eligible"
    if legal_decision_window_at < activation_at:
        return False, (
            f"the legal decision window {legal_decision_window_at.isoformat()} precedes the "
            f"Phase-2 activation instant {activation_at.isoformat()}"
        )
    return True, ""


def _json_safe(value: Any) -> Any:
    """Coerce a payload into something a JSON column will actually accept.

    Decimal, date and datetime all reach these dicts legitimately -- a strike,
    an expiration, a quote timestamp -- and psycopg raises on all three. A
    flush that fails inside a forward window would lose the whole decision, so
    the guarantee is enforced here rather than trusted to every caller. Values
    become strings, which is the convention the entry and settlement evidence
    already use, so nothing downstream has to learn a second encoding.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal | date | datetime):
        return str(value)
    return value


def _candidate_rows(
    evaluation: Phase2Evaluation,
    *,
    evidence: MoveEvidence,
    policy: ViabilityPolicy,
    settlement_date: date | None,
    chain_metadata_snapshot_id: int | None,
) -> list[dict]:
    """The complete bounded universe, refused candidates included."""
    accepted_ids = {
        candidate_id
        for decision in evaluation.configurations
        for candidate_id in decision.ranked_candidate_ids
    }
    rank_by_id: dict[str, int] = {}
    for decision in evaluation.configurations:
        for position, candidate_id in enumerate(decision.ranked_candidate_ids, start=1):
            # The best rank any configuration gave it. A per-configuration
            # rank lives on the configuration's own row; this is the
            # universe-level summary.
            existing = rank_by_id.get(candidate_id)
            if existing is None or position < existing:
                rank_by_id[candidate_id] = position

    rows: list[dict] = []
    for shared in evaluation.universe:
        detail = evaluation.candidate_detail.get(shared.candidate_id, {})
        context = detail.get("expiry_context", {})
        edge = evaluate_move_edge(shared.strategy, evidence, policy)
        expiration = detail.get("expiration")
        implied = context.get("implied_move_pct")
        rows.append(
            {
                "candidate_id": shared.candidate_id,
                "strategy": shared.strategy,
                "expiration": expiration,
                "expiry_ladder_position": detail.get("expiry_ladder_position"),
                "entry_dte": context.get("entry_dte"),
                "dte_at_settlement": (
                    context.get("dte_at_settlement")
                    if settlement_date is None or expiration is None
                    else (expiration - settlement_date).days
                ),
                "settlement_risk": context.get("settlement_risk"),
                "geometry_variant_id": detail.get("geometry_variant_id"),
                "expiry_implied_move_pct": None if implied is None else Decimal(str(implied)),
                "expiry_implied_move_source": context.get("implied_move_source"),
                "chain_metadata_snapshot_id": chain_metadata_snapshot_id,
                "semantic_compatibility": detail.get("semantic_compatibility"),
                "semantic_tier": detail.get("semantic_tier"),
                "core_median_return": shared.economics.median_return,
                "core_worst_return": shared.economics.worst_return,
                "core_best_return": shared.economics.best_return,
                "core_positive_scenario_fraction": shared.economics.positive_scenario_fraction,
                "no_profitable_region": shared.economics.no_profitable_region,
                "move_edge_status": edge.status,
                "move_edge_exposure": edge.exposure,
                "move_edge_ratio": edge.edge_ratio,
                "move_edge_threshold": edge.threshold,
                "move_edge_explanation": edge.explanation,
                "mean_relative_spread": shared.economics.mean_relative_spread,
                "worst_relative_spread": detail.get("worst_relative_spread"),
                "entry_cash_required": shared.entry_cash_required,
                "per_contract_max_risk": shared.per_contract_max_risk,
                "n_legs": shared.n_legs,
                "n_legs_with_two_sided_quote": shared.n_legs_with_two_sided_quote,
                "validity_status": detail.get("validity_status"),
                "validity_reason": detail.get("validity_reason"),
                "viability_acceptable": shared.candidate_id in accepted_ids,
                "rank": rank_by_id.get(shared.candidate_id),
                "legs_json": _json_safe({"legs": detail.get("legs", [])}),
                "market_data_quality": detail.get("market_data_quality"),
            }
        )
    return rows


def _config_rows(evaluation: Phase2Evaluation) -> list[dict]:
    rows: list[dict] = []
    for decision in evaluation.configurations:
        configuration = decision.configuration
        position = decision.position
        rejections = [
            {"candidate_id": r.candidate_id, "stage": r.stage, "detail": r.detail}
            for r in decision.rejections[:_MAX_PERSISTED_REJECTIONS]
        ]
        summary: dict[str, Any] = dict(decision.diagnostics.as_dict())
        summary["rejections_persisted"] = len(rejections)
        summary["rejections_total"] = len(decision.rejections)
        summary["rejections"] = rejections
        rows.append(
            {
                "configuration_key": configuration.key,
                "capital_base": configuration.capital_base,
                "risk_profile": configuration.risk_profile.value,
                "max_risk_dollars": configuration.max_risk_dollars,
                "status": (
                    _PERSISTED_ACTION if decision.status == STATUS_ACTION else decision.status
                ),
                "selected_candidate_id": decision.selected_candidate_id,
                "no_action_reason": decision.no_action_reason,
                "rank": decision.rank,
                "quantity": position.quantity if position else None,
                "per_contract_entry_cash": (
                    position.per_contract_entry_cash if position else None
                ),
                "per_contract_max_risk": position.per_contract_max_risk if position else None,
                "capital_used": position.capital_used if position else None,
                "max_risk_used": position.max_risk_used if position else None,
                "configuration_version": PHASE_2_VERSIONS.configuration_version,
                "ranking_version": decision.ranking_version,
                "rejection_summary": _json_safe(summary),
                "ranked_candidate_ids": list(decision.ranked_candidate_ids),
                "selection_explanation": decision.selection_explanation,
            }
        )
    return rows


def freeze_phase2_decision(
    db: Session,
    control: V4ShadowDecision,
    evaluation: Phase2Evaluation,
    *,
    observed_at: datetime | None = None,
    settlement_date: date | None = None,
    policy: ViabilityPolicy | None = None,
    chain_metadata_snapshot_id: int | None = None,
) -> Phase2FreezeResult:
    """Persist one Phase-2 decision, its complete universe and its six
    configuration results.

    Idempotent on (event, gate version, observed instant, methodology) and
    savepoint-isolated, for the reason Phase 1 documents: a bare rollback
    would unwind the caller's whole transaction, so a challenger fault could
    discard unrelated control work.
    """
    policy = policy or DEFAULT_POLICY
    observed_at = observed_at or control.generated_at

    existing = (
        db.query(V42ChallengerDecision)
        .filter_by(
            earnings_calendar_event_id=control.earnings_calendar_event_id,
            observed_at=observed_at,
            methodology_version=PHASE_2_METHODOLOGY,
        )
        .one_or_none()
    )
    if existing is not None:
        return Phase2FreezeResult(FREEZE_ALREADY_FROZEN, existing.id)

    distribution = evaluation.move_distribution
    evidence = MoveEvidence(
        implied_move_pct=evaluation.implied_move_pct, distribution=distribution
    )

    savepoint = db.begin_nested()
    try:
        row = V42ChallengerDecision(
            earnings_calendar_event_id=control.earnings_calendar_event_id,
            shadow_decision_id=control.id,
            ticker=control.ticker,
            generated_at=datetime.now(UTC),
            observed_at=observed_at,
            schema_version=CHALLENGER_SCHEMA_VERSION,
            methodology_version=PHASE_2_METHODOLOGY,
            candidate_universe_version=PHASE_2_VERSIONS.candidate_universe_version,
            configuration_version=PHASE_2_VERSIONS.configuration_version,
            strategy_registry_version=PHASE_2_VERSIONS.strategy_registry_version,
            timing_policy_version=PHASE_2_VERSIONS.timing_policy_version,
            gate_version=PHASE_2_VERSIONS.viability_gate_version,
            move_edge_version=PHASE_2_VERSIONS.move_edge_version,
            move_distribution_version=MOVE_DISTRIBUTION_VERSION,
            reaction_anchoring_version=REACTION_ANCHORING_VERSION,
            expiry_ladder_version=PHASE_2_VERSIONS.expiry_ladder_version,
            friction_version=PHASE_2_VERSIONS.friction_version,
            ranking_version=PHASE_2_VERSIONS.ranking_version,
            decision_view_schema_version=control.decision_view_schema_version,
            historical_sample_n=getattr(distribution, "sample_n", None),
            historical_evidence_quality=getattr(distribution, "quality", None),
            historical_timing_quality=getattr(distribution, "timing_quality", None),
            historical_median_abs_move_pct=getattr(distribution, "median_abs_move_pct", None),
            historical_p25_abs_move_pct=getattr(distribution, "p25_abs_move_pct", None),
            historical_p75_abs_move_pct=getattr(distribution, "p75_abs_move_pct", None),
            historical_source_digest=getattr(distribution, "source_digest", None),
            historical_source_event_count=getattr(distribution, "source_event_count", None),
            historical_as_of=getattr(distribution, "as_of", None),
            implied_move_pct=evaluation.implied_move_pct,
            underlying_price=evaluation.underlying_price,
            market_data_quality=evaluation.market_data_quality,
            status=evaluation.status,
            # A Phase-2 event has no single event-level winner, by design.
            # The six configuration rows are the decision; leaving this NULL
            # is the honest representation of that, not a missing value.
            selected_candidate_id=None,
            no_action_reason=_event_reason(evaluation),
            candidates_evaluated=len(evaluation.universe),
            candidates_accepted=sum(
                1 for c in evaluation.universe if _accepted_anywhere(evaluation, c.candidate_id)
            ),
            configurations_actioned=evaluation.action_count,
            distinct_selected_candidates=len(evaluation.selected_candidate_ids),
            total_latency_ms=evaluation.telemetry.total_latency_ms,
            metadata_request_count=_stage_requests(evaluation, "metadata"),
            contract_detail_request_count=_stage_requests(evaluation, "chain_discovery"),
            market_data_request_count=evaluation.telemetry.total_requests,
            unique_contracts_quoted=evaluation.telemetry.unique_contracts,
            reused_control_contracts=evaluation.telemetry.contracts_reused_from_control,
            request_budget=_json_safe(evaluation.telemetry.as_dict()),
            chain_metadata_snapshot_id=chain_metadata_snapshot_id,
            expiries_considered=evaluation.expiries_considered,
            multi_expiry_status=(
                evaluation.multi_expiry.status if evaluation.multi_expiry else None
            ),
            failure_category=evaluation.failure_category,
            failure_detail=evaluation.failure_detail,
        )
        db.add(row)
        db.flush()

        for candidate in _candidate_rows(
            evaluation,
            evidence=evidence,
            policy=policy,
            settlement_date=settlement_date,
            chain_metadata_snapshot_id=chain_metadata_snapshot_id,
        ):
            db.add(V42ChallengerCandidate(challenger_decision_id=row.id, **candidate))
        for config in _config_rows(evaluation):
            db.add(V42ChallengerConfigResult(challenger_decision_id=row.id, **config))
        db.flush()
        savepoint.commit()
        return Phase2FreezeResult(FREEZE_FROZEN, row.id)
    except IntegrityError as exc:
        savepoint.rollback()
        return Phase2FreezeResult(FREEZE_ALREADY_FROZEN, None, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 -- never reaches the control
        savepoint.rollback()
        log.error("phase-2 freeze failed for %s", control.ticker, exc_info=True)
        return Phase2FreezeResult(FREEZE_FAILED, None, f"{type(exc).__name__}: {exc}")


def _accepted_anywhere(evaluation: Phase2Evaluation, candidate_id: str) -> bool:
    return any(
        candidate_id in decision.ranked_candidate_ids for decision in evaluation.configurations
    )


def _stage_requests(evaluation: Phase2Evaluation, name: str) -> int:
    return sum(s.requests for s in evaluation.telemetry.stages if s.name == name)


def _event_reason(evaluation: Phase2Evaluation) -> str | None:
    """An event-level summary only when NO configuration acted. When some
    did, the per-configuration rows carry the reasons and a single event
    sentence would be a false generalisation."""
    if evaluation.failure_detail:
        return evaluation.failure_detail
    if evaluation.action_count:
        return None
    reasons = {c.no_action_reason for c in evaluation.configurations if c.no_action_reason}
    if not reasons:
        return None
    if len(reasons) == 1:
        return next(iter(reasons))
    return (
        f"all six configurations declined, for {len(reasons)} different reasons; "
        "see the per-configuration rows"
    )


def phase2_decisions_for_event(db: Session, earnings_calendar_event_id: int) -> list[Any]:
    return (
        db.query(V42ChallengerDecision)
        .filter_by(
            earnings_calendar_event_id=earnings_calendar_event_id,
            methodology_version=PHASE_2_METHODOLOGY,
        )
        .order_by(V42ChallengerDecision.id)
        .all()
    )


__all__ = [
    "FREEZE_ALREADY_FROZEN",
    "FREEZE_FAILED",
    "FREEZE_FROZEN",
    "FREEZE_NOT_ACTIVATED",
    "Phase2FreezeResult",
    "freeze_phase2_decision",
    "phase2_activated",
]
