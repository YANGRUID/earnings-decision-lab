"""V4.2 PHASE 2 -- honest replay classification, and the one replay that is
actually supported.

WHY THIS IS MOSTLY A REFUSAL
----------------------------
Phase 2's whole change is the SEARCH SPACE: several expiries, each with its
own listed strikes, its own ATM straddle and its own geometry. Replaying that
on a past event would require the listed metadata and the quotes for those
expiries as they stood at that event's decision instant. Measured: the
``v4_chain_metadata_snapshot`` table holds zero rows, and all 21 control
decisions froze exactly one expiry each. That evidence does not exist and
cannot be recovered.

Reconstructing it from today's chain would be worse than useless. Today's
board has different listed strikes, different open interest and a different
volatility surface, and the outcome of the event is already known -- a
"replay" built that way measures hindsight, not method. So it is not offered,
not behind a flag, and not as a diagnostic.

WHAT IS SUPPORTED, AND WHAT IT ANSWERS
--------------------------------------
Every event DOES have its single-expiry candidate set frozen with real
two-sided legs. Re-running the six configuration policies over that frozen
universe answers a narrower but genuinely useful question:

    of the change Phase 2 makes, how much comes from the CONFIGURATION
    POLICY alone, before a single extra expiry is searched?

That isolates the two halves of the change. It is labelled PARTIAL for
exactly that reason, and it is never presented as what Phase 2 would have
done -- Phase 2 would have searched a universe this evidence does not contain.

NO OUTCOME, ANYWHERE
--------------------
Nothing here reads a settlement, an entry, a realized P&L or an earnings
result. The classification and the partial replay touch decision, candidate
and leg rows only. A replay that could see the outcome would be a way to tune
the engine on it by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from analytics.decision.v4_2_config_policy import (
    STATUS_ACTION,
    ConfigurationDecision,
    SharedCandidate,
    decide_all_configurations,
)
from analytics.decision.v4_2_viability import CandidateEconomics, MoveEvidence
from analytics.decision.v4_configurations import V4_CONFIGURATIONS
from models.v4_2_challenger import V4ChainMetadataSnapshot
from models.v4_shadow import V4ShadowCandidate, V4ShadowCandidateLeg, V4ShadowDecision
from services.v4_2_challenger_entry import max_defined_risk_from_legs
from services.v4_2_move_history import anchored_move_distribution_for_ticker

REPLAY_FULL = "FULL_REPLAY_SUPPORTED"
REPLAY_PARTIAL = "PARTIAL_REPLAY_SUPPORTED"
REPLAY_NONE = "CANNOT_REPLAY_HONESTLY"


@dataclass(frozen=True)
class ReplayClassification:
    ticker: str
    shadow_decision_id: int
    mode: str
    reason: str
    frozen_expiries: int = 0
    frozen_candidates: int = 0
    candidates_with_two_sided_legs: int = 0
    has_chain_metadata_snapshot: bool = False

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "shadow_decision_id": self.shadow_decision_id,
            "mode": self.mode,
            "reason": self.reason,
            "frozen_expiries": self.frozen_expiries,
            "frozen_candidates": self.frozen_candidates,
            "candidates_with_two_sided_legs": self.candidates_with_two_sided_legs,
            "has_chain_metadata_snapshot": self.has_chain_metadata_snapshot,
        }


@dataclass
class PartialReplay:
    """The configuration policy alone, over one event's frozen universe."""

    ticker: str
    shadow_decision_id: int
    universe: int = 0
    decisions: list[ConfigurationDecision] = field(default_factory=list)

    @property
    def action_count(self) -> int:
        return sum(1 for d in self.decisions if d.status == STATUS_ACTION)

    @property
    def distinct_selections(self) -> int:
        return len({d.selected_candidate_id for d in self.decisions if d.selected_candidate_id})


def classify_replay(db: Session, control: V4ShadowDecision) -> ReplayClassification:
    """What can honestly be replayed for ONE past event."""
    candidates = (
        db.query(V4ShadowCandidate).filter_by(shadow_decision_id=control.id).all()
    )
    expiries = {c.expiration for c in candidates if c.expiration is not None}
    legs_by_candidate: dict[int, list[V4ShadowCandidateLeg]] = {}
    if candidates:
        for leg in db.query(V4ShadowCandidateLeg).filter(
            V4ShadowCandidateLeg.shadow_candidate_id.in_([c.id for c in candidates])
        ):
            legs_by_candidate.setdefault(leg.shadow_candidate_id, []).append(leg)
    priceable = sum(
        1
        for c in candidates
        if legs_by_candidate.get(c.id)
        and all(
            (leg.ask if leg.action == "buy" else leg.bid) is not None
            for leg in legs_by_candidate[c.id]
        )
    )
    snapshot = (
        db.query(V4ChainMetadataSnapshot)
        .filter_by(earnings_calendar_event_id=control.earnings_calendar_event_id)
        .first()
    )

    if snapshot is not None and len(expiries) > 1:
        mode, reason = (
            REPLAY_FULL,
            f"listed metadata and {len(expiries)} frozen expiries are available, so the "
            "multi-expiry universe can be rebuilt from point-in-time evidence",
        )
    elif priceable:
        mode, reason = (
            REPLAY_PARTIAL,
            f"{priceable} candidates on {len(expiries)} frozen expiry(ies) with executable "
            "legs: the six configuration policies can be replayed, the multi-expiry search "
            "cannot -- no listed metadata was frozen for this event",
        )
    else:
        mode, reason = (
            REPLAY_NONE,
            "no frozen candidate carries an executable entry side, so nothing can be "
            "re-decided without inventing a price",
        )

    return ReplayClassification(
        ticker=control.ticker,
        shadow_decision_id=control.id,
        mode=mode,
        reason=reason,
        frozen_expiries=len(expiries),
        frozen_candidates=len(candidates),
        candidates_with_two_sided_legs=priceable,
        has_chain_metadata_snapshot=snapshot is not None,
    )


def _shared_from_frozen(
    candidate: V4ShadowCandidate, legs: list[V4ShadowCandidateLeg]
) -> SharedCandidate:
    two_sided = sum(1 for leg in legs if leg.bid is not None and leg.ask is not None)
    missing_side = any(
        (leg.ask if leg.action == "buy" else leg.bid) is None for leg in legs
    )
    return SharedCandidate(
        candidate_id=candidate.candidate_id,
        strategy=candidate.strategy,
        economics=CandidateEconomics(
            candidate_id=candidate.candidate_id,
            strategy=candidate.strategy,
            median_return=candidate.core_median_return,
            worst_return=candidate.core_worst_return,
            best_return=candidate.core_best_return,
            positive_scenario_fraction=candidate.core_positive_scenario_fraction,
            no_profitable_region=candidate.no_profitable_region,
            semantic_compatibility=candidate.semantic_compatibility,
            mean_relative_spread=candidate.mean_relative_spread,
        ),
        entry_cash_required=candidate.entry_cash_required,
        per_contract_max_risk=max_defined_risk_from_legs(legs),
        n_legs=len(legs),
        n_legs_with_two_sided_quote=two_sided,
        data_invalid_reason=(
            "required entry side missing on a frozen leg" if missing_side or not legs else None
        ),
    )


def replay_configuration_policy(db: Session, control: V4ShadowDecision) -> PartialReplay:
    """Re-run the six configuration policies over ONE event's frozen,
    single-expiry universe.

    Reads the control's OWN frozen valuations -- no candidate is re-priced,
    because re-pricing a past event needs a past chain. The ONLY thing that
    varies from what actually happened is the configuration policy.

    Note the deliberate difference from Phase 1's own record: this includes
    candidates V4.1 marked CAPITAL_INCOMPATIBLE, because that verdict is
    relative to a $2,000 standardized capital and three configurations hold
    $10,000. Excluding them would replay the very defect under study.
    """
    candidates = db.query(V4ShadowCandidate).filter_by(shadow_decision_id=control.id).all()
    legs_by_candidate: dict[int, list[V4ShadowCandidateLeg]] = {}
    if candidates:
        for leg in (
            db.query(V4ShadowCandidateLeg)
            .filter(V4ShadowCandidateLeg.shadow_candidate_id.in_([c.id for c in candidates]))
            .order_by(V4ShadowCandidateLeg.leg_index)
        ):
            legs_by_candidate.setdefault(leg.shadow_candidate_id, []).append(leg)

    universe = [
        _shared_from_frozen(candidate, legs_by_candidate.get(candidate.id, []))
        for candidate in candidates
    ]
    distribution = anchored_move_distribution_for_ticker(
        db, ticker=control.ticker, as_of=control.generated_at.date()
    )
    move = control.expected_move if isinstance(control.expected_move, dict) else {}
    implied = move.get("implied_move_pct")
    evidence = MoveEvidence(
        implied_move_pct=None if implied is None else Decimal(str(implied)),
        distribution=distribution,
    )
    return PartialReplay(
        ticker=control.ticker,
        shadow_decision_id=control.id,
        universe=len(universe),
        decisions=decide_all_configurations(universe, V4_CONFIGURATIONS, evidence=evidence),
    )


def replay_report(db: Session, *, limit: int = 100) -> dict:
    """Classify every past control decision, and where a partial replay is
    supported, report what the configuration policy alone would have chosen.

    Zero-outcome by construction: no settlement, entry or realized figure is
    read anywhere in this module.
    """
    controls = (
        db.query(V4ShadowDecision).order_by(V4ShadowDecision.id.desc()).limit(limit).all()
    )
    events: list[dict] = []
    counts = {REPLAY_FULL: 0, REPLAY_PARTIAL: 0, REPLAY_NONE: 0}
    for control in controls:
        classification = classify_replay(db, control)
        counts[classification.mode] = counts.get(classification.mode, 0) + 1
        entry: dict[str, Any] = classification.as_dict()
        if classification.mode in (REPLAY_FULL, REPLAY_PARTIAL):
            replay = replay_configuration_policy(db, control)
            entry["configuration_policy_replay"] = {
                "universe": replay.universe,
                "configurations_actioned": replay.action_count,
                "distinct_selections": replay.distinct_selections,
                "configurations": [
                    {
                        "configuration_key": d.configuration.key,
                        "status": d.status,
                        "selected_candidate_id": d.selected_candidate_id,
                        "reason": d.no_action_reason or d.selection_explanation,
                        "diagnostics": d.diagnostics.as_dict(),
                    }
                    for d in replay.decisions
                ],
            }
        events.append(entry)

    return {
        "mode": "ZERO_OUTCOME_REPLAY",
        "notice": (
            "Classification and configuration-policy replay only. No realized outcome, "
            "settlement or P&L is read. A multi-expiry replay is not offered for any past "
            "event: the listed metadata and per-expiry quotes were never frozen, and "
            "rebuilding them from today's chain would measure hindsight rather than method."
        ),
        "counts": counts,
        "events": events,
    }


__all__ = [
    "REPLAY_FULL",
    "REPLAY_NONE",
    "REPLAY_PARTIAL",
    "PartialReplay",
    "ReplayClassification",
    "classify_replay",
    "replay_configuration_policy",
    "replay_report",
]
