"""Phase 4.3 -- connects earnings_calendar_event -> eligibility ->
timing gate -> AI Decision Engine -> decision_snapshot into one real
pipeline. See PHASE4.3_ARCHITECTURE_REVIEW.md sec 1 for the full data
flow this implements, and sec 3 for the look-ahead-bias reasoning behind
the timing/lateness checks here.

Only the creation flow -- no entry capture, no settlement, no P&L, no
frontend (all explicitly out of Phase 4.3's scope).

Phase 4.3 decision #4: an ineligible or not-yet-due event is never
written back to earnings_calendar_event (that table is not modified by
this module at all) -- the outcome is returned as a real, structured
value and logged, which is this phase's own way of "handling the
analysis outcome" without a persisted SKIPPED/ANALYZED status anywhere.
The existence of a decision_snapshot row *is* the durable record that an
event was successfully analyzed; there is deliberately no durable record
of a skip beyond the application log.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from analytics.earnings_timing import compute_entry_exit_schedule
from models.benchmark_portfolio import BenchmarkPortfolio
from models.company import Company
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import AnnouncementTime, EarningsTiming, RiskProfile
from providers.base import OptionsDataProvider
from rag.embeddings import EmbeddingProvider
from services.decision_engine import generate_decision
from services.decision_snapshot_freezing import freeze_decision_snapshot
from services.earnings_eligibility import check_eligibility
from services.llm.base import LLMProvider

log = logging.getLogger("services.decision_pipeline")

# benchmark_portfolio has no risk_profile column yet -- not part of this
# phase's migration scope (see 201cc8a16cb0's own note). Every decision
# this pipeline generates uses this fixed default until a later
# migration adds a real per-portfolio setting.
DEFAULT_RISK_PROFILE = RiskProfile.MODERATE

# How long past the scheduled entry_timestamp a generation attempt is
# still considered safe -- see PHASE4.3_ARCHITECTURE_REVIEW.md sec 3.
# entry_timestamp is always 15:55 ET; real US market close is 16:00 ET,
# so this grace window ends exactly at close, the real moment the market
# could start reacting -- not an arbitrary buffer.
LATE_CUTOFF_GRACE = timedelta(minutes=5)

Outcome = Literal[
    "created",
    "already_frozen",
    "skipped_ineligible",
    "skipped_not_due",
    "skipped_too_late",
    "skipped_no_company",
    "failed",
]

_TIMING_TO_ANNOUNCEMENT_TIME: dict[EarningsTiming, AnnouncementTime] = {
    EarningsTiming.BMO: AnnouncementTime.BEFORE_MARKET,
    EarningsTiming.AMC: AnnouncementTime.AFTER_MARKET,
    # DMH has no AnnouncementTime equivalent -- mapped to UNKNOWN so
    # compute_entry_exit_schedule takes its own documented conservative
    # (BMO-shaped) branch for it, exactly as that module's docstring
    # already prescribes for any session it doesn't specifically know.
    EarningsTiming.DMH: AnnouncementTime.UNKNOWN,
    EarningsTiming.UNKNOWN: AnnouncementTime.UNKNOWN,
}


@dataclass(frozen=True)
class DecisionPipelineOutcome:
    calendar_event_id: int
    symbol: str
    outcome: Outcome
    decision_snapshot_id: int | None = None
    reason: str | None = None


def run_decision_pipeline_for_event(
    db: Session,
    calendar_event: EarningsCalendarEvent,
    portfolio: BenchmarkPortfolio,
    options_provider: OptionsDataProvider | None,
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    *,
    now: datetime | None = None,
) -> DecisionPipelineOutcome:
    """The one real per-event pipeline call -- idempotent (an
    already-frozen event is a cheap DB lookup, never a second live
    generation), never modifies earnings_calendar_event, never raises for
    an ineligible/not-yet-due/already-frozen event (only a genuine
    generate_decision() failure produces "failed", and even that is
    caught and returned, not raised, so a scheduler loop over many events
    can't be aborted by one company's failure)."""
    now = now or datetime.now(UTC)

    existing = (
        db.query(DecisionSnapshot)
        .filter_by(
            earnings_calendar_event_id=calendar_event.id, benchmark_portfolio_id=portfolio.id
        )
        .one_or_none()
    )
    if existing is not None:
        return DecisionPipelineOutcome(
            calendar_event.id, calendar_event.symbol, "already_frozen", existing.id
        )

    eligibility = check_eligibility(calendar_event, options_provider)
    if not eligibility.eligible:
        log.info(
            "decision pipeline: %s skipped (ineligible: %s)",
            calendar_event.symbol,
            eligibility.reason,
        )
        return DecisionPipelineOutcome(
            calendar_event.id,
            calendar_event.symbol,
            "skipped_ineligible",
            reason=eligibility.reason,
        )

    session = _TIMING_TO_ANNOUNCEMENT_TIME[calendar_event.earnings_time]
    schedule = compute_entry_exit_schedule(calendar_event.earnings_date, session)

    if now < schedule.entry_timestamp:
        return DecisionPipelineOutcome(
            calendar_event.id,
            calendar_event.symbol,
            "skipped_not_due",
            reason=f"generation scheduled for {schedule.entry_timestamp.isoformat()}",
        )
    if now > schedule.entry_timestamp + LATE_CUTOFF_GRACE:
        # Real risk, not hypothetical: a scheduler that fires late would
        # otherwise silently generate a "pre-earnings" decision using
        # data that may have already priced in the reaction. Skipped,
        # never generated anyway with a false pre-earnings label.
        log.warning(
            "decision pipeline: %s skipped (too late: now=%s, entry_timestamp=%s)",
            calendar_event.symbol,
            now.isoformat(),
            schedule.entry_timestamp.isoformat(),
        )
        return DecisionPipelineOutcome(
            calendar_event.id,
            calendar_event.symbol,
            "skipped_too_late",
            reason=(
                f"now ({now.isoformat()}) is past the safe window for entry_timestamp "
                f"{schedule.entry_timestamp.isoformat()}"
            ),
        )

    company = db.query(Company).filter(Company.ticker == calendar_event.symbol).one_or_none()
    if company is None:
        log.info(
            "decision pipeline: %s skipped (no researched company on record)",
            calendar_event.symbol,
        )
        return DecisionPipelineOutcome(
            calendar_event.id,
            calendar_event.symbol,
            "skipped_no_company",
            reason="no researched company on record for this symbol",
        )

    try:
        result = generate_decision(
            db, llm, embedder, company, risk_profile=DEFAULT_RISK_PROFILE
        )
        snapshot = freeze_decision_snapshot(
            db,
            calendar_event=calendar_event,
            portfolio=portfolio,
            company=company,
            result=result,
        )
    except Exception as exc:
        log.error(
            "decision pipeline: %s failed to freeze", calendar_event.symbol, exc_info=True
        )
        return DecisionPipelineOutcome(
            calendar_event.id, calendar_event.symbol, "failed", reason=str(exc)
        )

    log.info(
        "decision pipeline: %s frozen as decision_snapshot id=%s",
        calendar_event.symbol,
        snapshot.id,
    )
    return DecisionPipelineOutcome(
        calendar_event.id, calendar_event.symbol, "created", snapshot.id
    )
