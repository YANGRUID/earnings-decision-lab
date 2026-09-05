"""Control vs challenger, per event.

Deliberately neutral language throughout: CONTROL and CHALLENGER, never
"better", "winner" or "improved". Before forward outcomes exist there is
nothing to be better at, and the comparison's whole value depends on it not
quietly becoming an argument.

The comparison unit is the EVENT. The six configurations are sizing variants
of one market view, not six independent forecasts, so they are reported
beneath an event rather than alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from models.v4_2_challenger import (
    V4ChainMetadataSnapshot,
    V42ChallengerCandidate,
    V42ChallengerConfigResult,
    V42ChallengerDecision,
)
from models.v4_shadow import V4ShadowCandidate, V4ShadowConfigResult, V4ShadowDecision
from services.v4_2_move_history import anchored_move_distribution_for_ticker

#: Challenger evidence readiness, reported per event so an operator can see
#: what a parallel run would actually have to work with.
EVIDENCE_READY = "READY"
EVIDENCE_PARTIAL = "PARTIAL"
EVIDENCE_MISSING = "MISSING"


@dataclass
class MethodologySide:
    """One methodology's answer for one event."""

    methodology: str
    status: str | None = None
    selected_candidate_id: str | None = None
    strategy: str | None = None
    expiration: str | None = None
    median_return: Decimal | None = None
    worst_return: Decimal | None = None
    positive_scenario_fraction: Decimal | None = None
    no_action_reason: str | None = None
    candidates_evaluated: int | None = None
    candidates_accepted: int | None = None


@dataclass
class EventComparison:
    ticker: str
    earnings_calendar_event_id: int
    observed_at: str | None
    control: MethodologySide
    challenger: MethodologySide
    challenger_evidence: dict = field(default_factory=dict)
    configurations: list[dict] = field(default_factory=list)
    differs: bool = False


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _control_side(db: Session, decision: V4ShadowDecision) -> MethodologySide:
    selected = (
        db.query(V4ShadowCandidate)
        .filter_by(shadow_decision_id=decision.id, candidate_id=decision.rank_1_candidate_id)
        .one_or_none()
        if decision.rank_1_candidate_id
        else None
    )
    return MethodologySide(
        methodology="V4.1 CONTROL",
        status=decision.status,
        selected_candidate_id=decision.rank_1_candidate_id,
        strategy=selected.strategy if selected else None,
        expiration=selected.expiration.isoformat() if selected else None,
        median_return=_decimal(selected.core_median_return) if selected else None,
        worst_return=_decimal(selected.core_worst_return) if selected else None,
        positive_scenario_fraction=(
            _decimal(selected.core_positive_scenario_fraction) if selected else None
        ),
        no_action_reason=decision.no_action_reason,
        candidates_evaluated=decision.candidate_count,
        candidates_accepted=decision.rankable_candidate_count,
    )


def _challenger_side(
    db: Session, challenger: V42ChallengerDecision | None
) -> MethodologySide:
    if challenger is None:
        return MethodologySide(methodology="V4.2 CHALLENGER", status=None)
    selected = (
        db.query(V42ChallengerCandidate)
        .filter_by(
            challenger_decision_id=challenger.id,
            candidate_id=challenger.selected_candidate_id,
        )
        .one_or_none()
        if challenger.selected_candidate_id
        else None
    )
    return MethodologySide(
        methodology="V4.2 CHALLENGER",
        status=challenger.status,
        selected_candidate_id=challenger.selected_candidate_id,
        strategy=selected.strategy if selected else None,
        expiration=selected.expiration.isoformat() if selected else None,
        median_return=_decimal(selected.core_median_return) if selected else None,
        worst_return=_decimal(selected.core_worst_return) if selected else None,
        positive_scenario_fraction=(
            _decimal(selected.core_positive_scenario_fraction) if selected else None
        ),
        no_action_reason=challenger.no_action_reason,
        candidates_evaluated=challenger.candidates_evaluated,
        candidates_accepted=challenger.candidates_accepted,
    )


def _evidence_readiness(
    db: Session,
    decision: V4ShadowDecision,
    challenger: V42ChallengerDecision | None,
    chain: V4ChainMetadataSnapshot | None,
) -> dict:
    """What a challenger run had -- or, when none has been frozen, what one
    WOULD have to work with.

    The prospective form is the useful one for an operator deciding whether a
    parallel run is worth activating: reporting MISSING merely because no
    decision exists yet would describe the absence of a run rather than the
    absence of evidence.
    """
    sample_n: int | None = None
    timing_quality: str | None = None
    if challenger is not None:
        sample_n = challenger.historical_sample_n
        timing_quality = challenger.historical_timing_quality
    else:
        distribution = anchored_move_distribution_for_ticker(
            db, ticker=decision.ticker, as_of=decision.generated_at.date()
        )
        sample_n = distribution.sample_n
        timing_quality = distribution.timing_quality
    historical = EVIDENCE_READY if (sample_n or 0) > 0 else EVIDENCE_MISSING
    multi_expiry = EVIDENCE_READY if chain is not None else EVIDENCE_MISSING
    overall = (
        EVIDENCE_READY
        if historical == EVIDENCE_READY and multi_expiry == EVIDENCE_READY
        else (EVIDENCE_PARTIAL if EVIDENCE_READY in (historical, multi_expiry)
              else EVIDENCE_MISSING)
    )
    return {
        "historical_move": historical,
        "historical_sample_n": sample_n,
        "historical_timing_quality": timing_quality,
        "historical_source": "frozen" if challenger is not None else "prospective",
        "multi_expiry_metadata": multi_expiry,
        # The seven pre-Phase-2 events have no frozen chain, and no current
        # chain may stand in for one.
        "multi_expiry_replay": (
            "AVAILABLE" if chain is not None else "CANNOT_REPLAY_HONESTLY"
        ),
        "overall": overall,
    }


def compare_event(db: Session, decision: V4ShadowDecision) -> EventComparison:
    challenger = (
        db.query(V42ChallengerDecision)
        .filter_by(shadow_decision_id=decision.id)
        .order_by(V42ChallengerDecision.id.desc())
        .first()
    )
    chain = (
        db.query(V4ChainMetadataSnapshot)
        .filter_by(earnings_calendar_event_id=decision.earnings_calendar_event_id)
        .order_by(V4ChainMetadataSnapshot.id.desc())
        .first()
    )
    control = _control_side(db, decision)
    challenger_side = _challenger_side(db, challenger)

    configurations: list[dict] = []
    control_configs = {
        r.configuration_key: r
        for r in db.query(V4ShadowConfigResult).filter_by(shadow_decision_id=decision.id)
    }
    challenger_configs: dict[str, Any] = (
        {
            r.configuration_key: r
            for r in db.query(V42ChallengerConfigResult).filter_by(
                challenger_decision_id=challenger.id
            )
        }
        if challenger is not None
        else {}
    )
    for key in sorted(set(control_configs) | set(challenger_configs)):
        control_row = control_configs.get(key)
        challenger_row = challenger_configs.get(key)
        configurations.append(
            {
                "configuration_key": key,
                "control_status": control_row.status if control_row else None,
                "control_candidate_id": (
                    control_row.rank_1_candidate_id if control_row else None
                ),
                "challenger_status": challenger_row.status if challenger_row else None,
                "challenger_candidate_id": (
                    challenger_row.selected_candidate_id if challenger_row else None
                ),
                "challenger_no_action_reason": (
                    challenger_row.no_action_reason if challenger_row else None
                ),
            }
        )

    return EventComparison(
        ticker=decision.ticker,
        earnings_calendar_event_id=decision.earnings_calendar_event_id,
        observed_at=(
            challenger.observed_at.isoformat()
            if challenger is not None
            else decision.generated_at.isoformat()
        ),
        control=control,
        challenger=challenger_side,
        challenger_evidence=_evidence_readiness(db, decision, challenger, chain),
        configurations=configurations,
        # An absent challenger evaluation is not a disagreement. Reporting it
        # as one would inflate every "differs" count with events the
        # challenger never saw.
        differs=(
            challenger is not None
            and control.selected_candidate_id != challenger_side.selected_candidate_id
        ),
    )


def compare_all_events(db: Session) -> list[EventComparison]:
    return [
        compare_event(db, d)
        for d in db.query(V4ShadowDecision).order_by(V4ShadowDecision.id).all()
    ]
