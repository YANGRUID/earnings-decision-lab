"""Builds the V4.2 earnings-friction cohort from evidence V4 already froze.

No new market-data collection is required: every V4 candidate leg persists
its own real entry bid and ask, and the candidate persists its expiration.
That is exactly the short-dated-earnings universe the incumbent friction
model was missing, and it accumulates on its own with every forward event.
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.decision.v4_2_earnings_friction import (
    EarningsFrictionCohort,
    FrictionObservation,
    build_earnings_friction_cohort,
)
from models.v4_shadow import V4ShadowCandidate, V4ShadowCandidateLeg, V4ShadowDecision


def collect_friction_observations(db: Session) -> tuple[list[FrictionObservation], int]:
    """Every real two-sided leg quote V4 has observed at a decision instant."""
    rows = (
        db.query(V4ShadowCandidateLeg, V4ShadowCandidate, V4ShadowDecision)
        .join(V4ShadowCandidate, V4ShadowCandidateLeg.shadow_candidate_id == V4ShadowCandidate.id)
        .join(V4ShadowDecision, V4ShadowCandidate.shadow_decision_id == V4ShadowDecision.id)
        .filter(V4ShadowCandidateLeg.bid.isnot(None), V4ShadowCandidateLeg.ask.isnot(None))
        .all()
    )
    observations: list[FrictionObservation] = []
    events: set[int] = set()
    for leg, candidate, decision in rows:
        mid = (leg.bid + leg.ask) / 2
        if mid <= 0:
            # A zero mid has no relative spread to speak of -- excluded rather
            # than recorded as an infinite or zero one.
            continue
        spot = decision.underlying_price
        observations.append(
            FrictionObservation(
                relative_spread=(leg.ask - leg.bid) / mid,
                absolute_spread=leg.ask - leg.bid,
                dte=(candidate.expiration - decision.generated_at.date()).days,
                moneyness=(leg.strike / spot) if spot else None,
                right=leg.right,
                expiration=candidate.expiration,
                market_data_quality=leg.market_data_quality,
                volume=leg.volume,
                open_interest=leg.open_interest,
            )
        )
        events.add(decision.id)
    return observations, len(events)


def earnings_friction_cohort(db: Session) -> EarningsFrictionCohort:
    observations, events = collect_friction_observations(db)
    return build_earnings_friction_cohort(observations, distinct_events=events)


__all__ = ["Decimal", "collect_friction_observations", "earnings_friction_cohort"]
