"""Post-earnings settlement for the AI Options Decision journal (Phase
14.9 Part G). Runs once real post-earnings price data exists for the
first earnings event that occurred on or after a decision was generated,
and computes deterministic outcome fields directly on that same
AIDecisionVersion row -- never a new row (settlement is a factual update
to a real, already-generated decision, not a new research artifact; see
services/decision_history.py's module docstring for why this is not a
point-in-time-research violation).

Strictly honest about what this project can and cannot know:
- Directional accuracy and breakeven success are always computable once a
  real PriceReaction exists (they only need the real next-day move and
  the real, already-computed strategy breakevens/net_premium).
- Actual options P&L is NEVER computed. This project does not capture
  real point-in-time entry/exit option premiums for expired contracts
  (see docs/limitations.md and models/ai_decision_version.py) --
  strategy_pnl stays null and strategy_pnl_available stays False for
  every settlement this function ever produces. Do not add strategy P&L
  computation here without first building that real data pipeline.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy.orm import Session

from models.ai_decision_version import AIDecisionVersion
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.earnings_event import EarningsEvent
from models.enums import DecisionDirection, DecisionStatus
from models.price_reaction import PriceReaction

_DIRECTION_SIGN: dict[DecisionDirection, int] = {
    DecisionDirection.STRONG_BULLISH: 1,
    DecisionDirection.BULLISH: 1,
    DecisionDirection.NEUTRAL: 0,
    DecisionDirection.BEARISH: -1,
    DecisionDirection.STRONG_BEARISH: -1,
}


def find_settlement_event(db: Session, decision: AIDecisionVersion) -> EarningsEvent | None:
    """The earliest real EarningsEvent for this decision's company with a
    real, non-null next_day_move_pct, reported on or after the decision
    was generated. Returns None -- never a guess -- when no such event
    has been reported yet."""
    decision_date = decision.created_at.date()
    return (
        db.query(EarningsEvent)
        .join(PriceReaction, PriceReaction.earnings_event_id == EarningsEvent.id)
        .filter(
            EarningsEvent.company_id == decision.company_id,
            EarningsEvent.earnings_date.isnot(None),
            EarningsEvent.earnings_date >= decision_date,
            PriceReaction.next_day_move_pct.isnot(None),
        )
        .order_by(EarningsEvent.earnings_date.asc())
        .first()
    )


SettlementState = Literal[
    "not_final",
    "earnings_date_unknown",
    "waiting_for_event",
    "waiting_for_post_event_price",
    "ready",
    "settled",
]


@dataclass(frozen=True)
class SettlementEligibility:
    """The real, deterministic reason "Attempt Settlement" either works or
    doesn't right now (Phase 14.10 Part I1) -- computed fresh on every
    read, never persisted, so it's always in sync with real data as it
    arrives. Replaces the prior behavior where every failure mode
    (unknown earnings date, event hasn't happened, no post-event price
    yet) silently collapsed into settle_decision() returning None and the
    API endpoint discarding that -- see git history for the real root
    cause this was built to fix.
    """

    eligible: bool
    state: SettlementState
    reason: str
    earliest_settlement_date: date | None = None
    missing_requirements: list[str] = field(default_factory=list)


def compute_settlement_eligibility(
    db: Session, decision: AIDecisionVersion
) -> SettlementEligibility:
    if decision.status == DecisionStatus.SETTLED:
        return SettlementEligibility(
            eligible=False, state="settled", reason="This decision has already been settled."
        )

    if not decision.is_final:
        return SettlementEligibility(
            eligible=False,
            state="not_final",
            reason="Only the Final Decision can be settled -- mark this version as final first.",
            missing_requirements=["is_final"],
        )

    target_date: date | None = None
    if decision.earnings_estimate_snapshot_id is not None:
        snapshot = db.get(EarningsEstimateSnapshot, decision.earnings_estimate_snapshot_id)
        if snapshot is not None:
            target_date = snapshot.estimated_report_date

    if target_date is None:
        return SettlementEligibility(
            eligible=False,
            state="earnings_date_unknown",
            reason="No upcoming earnings date was known when this decision was generated -- "
            "settlement needs a real reported earnings date to find the right price reaction.",
            missing_requirements=["earnings_date"],
        )

    today = datetime.now(UTC).date()
    if target_date > today:
        return SettlementEligibility(
            eligible=False,
            state="waiting_for_event",
            reason=f"Expected earnings around {target_date.isoformat()}. This decision can be "
            "evaluated after the event and post-earnings price data is available.",
            earliest_settlement_date=target_date + timedelta(days=1),
            missing_requirements=["earnings_event"],
        )

    event = find_settlement_event(db, decision)
    reaction = event.price_reaction if event is not None else None
    if reaction is None or reaction.next_day_move_pct is None:
        return SettlementEligibility(
            eligible=False,
            state="waiting_for_post_event_price",
            reason="The earnings date has passed, but real post-earnings price data hasn't been "
            "ingested yet -- refresh this company's research once it's available.",
            missing_requirements=["post_event_price"],
        )

    return SettlementEligibility(eligible=True, state="ready", reason="Ready to settle.")


def _direction_correct(direction: DecisionDirection, actual_move_pct: Decimal) -> bool | None:
    """None for a neutral view -- "was the direction correct" has no
    clean binary answer when no direction was called. Bullish/bearish
    views are graded purely on the real sign of the real move."""
    sign = _DIRECTION_SIGN[direction]
    if sign == 0:
        return None
    return (actual_move_pct > 0) == (sign > 0)


def _breakeven_met(decision: AIDecisionVersion, actual_move_pct: Decimal) -> bool | None:
    """None when this decision had no recommended strategy, no real
    underlying price on record, or no real breakevens to compare against
    -- never guessed."""
    analysis = decision.recommended_strategy_analysis
    underlying_price = decision.underlying_price
    if analysis is None or underlying_price is None:
        return None
    breakevens_raw = analysis.get("breakevens") or []
    if not breakevens_raw:
        return None
    breakevens = [Decimal(b) for b in breakevens_raw]
    actual_price = underlying_price * (1 + actual_move_pct)
    nearest = min(breakevens, key=lambda be: abs(be - underlying_price))
    net_premium = Decimal(analysis["net_premium"])
    requires_move_beyond = net_premium >= 0  # a debit position needs to clear its breakeven
    if requires_move_beyond:
        if nearest >= underlying_price:
            return actual_price >= nearest
        return actual_price <= nearest
    # A credit position profits by staying within its breakeven.
    if nearest >= underlying_price:
        return actual_price < nearest
    return actual_price > nearest


def settle_decision(db: Session, decision: AIDecisionVersion) -> AIDecisionVersion | None:
    """Attempts to settle ``decision``. Returns the (updated) row when
    settlement happened, or None when there's no real post-earnings data
    yet to settle against -- callers should treat None as "try again
    later", never as an error. Idempotent: calling this again on an
    already-settled decision is a no-op that returns the row unchanged."""
    if decision.status == DecisionStatus.SETTLED:
        return decision

    event = find_settlement_event(db, decision)
    if event is None or event.price_reaction is None:
        return None
    reaction = event.price_reaction
    if reaction.next_day_move_pct is None:
        return None

    actual_next_day_move_pct = reaction.next_day_move_pct
    direction = DecisionDirection(decision.direction)

    decision.earnings_event_id = event.id
    decision.actual_next_day_move_pct = actual_next_day_move_pct
    decision.actual_five_day_move_pct = reaction.five_day_move_pct
    decision.direction_correct = _direction_correct(direction, actual_next_day_move_pct)
    decision.actual_move_exceeded_implied = (
        abs(actual_next_day_move_pct) > decision.implied_move_pct
        if decision.implied_move_pct is not None
        else None
    )
    decision.breakeven_met = _breakeven_met(decision, actual_next_day_move_pct)
    # Never computed -- see module docstring. Explicit, not merely
    # defaulted, so a future reader can't mistake "never set" for "forgot
    # to set."
    decision.strategy_pnl = None
    decision.strategy_pnl_available = False
    decision.status = DecisionStatus.SETTLED
    decision.settled_at = datetime.now(UTC)

    db.add(decision)
    db.commit()
    db.refresh(decision)
    return decision


def settle_all_pending(db: Session, *, limit: int = 200) -> list[AIDecisionVersion]:
    """Attempts settlement for every OPEN decision, in creation order.
    Bounded by ``limit`` -- this project is a personal research tool with
    a small real decision count, never a batch job over an unbounded
    table (see services/research_history.py's DEFAULT/MAX_HISTORY_LIMIT
    precedent for the same reasoning)."""
    pending = (
        db.query(AIDecisionVersion)
        .filter(AIDecisionVersion.status == DecisionStatus.OPEN)
        .order_by(AIDecisionVersion.created_at.asc())
        .limit(limit)
        .all()
    )
    settled = []
    for decision in pending:
        result = settle_decision(db, decision)
        if result is not None:
            settled.append(result)
    return settled
