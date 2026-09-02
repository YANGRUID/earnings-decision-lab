"""Operations Monitor -- real, persisted scheduler job execution history
(see models/scheduler_run.py for why this exists at all: nothing about
job execution was durably recorded before this).

Every function here is pure bookkeeping around the real, existing job
bodies in services/scheduler.py -- none of them decide anything about
eligibility, decision generation, entry pricing, or settlement pricing.
A tracking write is a separate, immediate commit rather than being
folded into whatever transaction the surrounding job body is managing:
that keeps a scheduler_run/scheduler_run_event row durable even when the
real trading-side operation it's describing rolls back (a FAILED entry
capture is exactly the kind of thing this page most needs to show, so
its own observability record must survive the rollback that correctly
discards the failed attempt's other pending writes).
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from models.scheduler_run import SchedulerRun, SchedulerRunEvent

RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_ERROR = "error"
RUN_STATUS_SKIPPED = "skipped"

# Post-official-run cleanup (2026-08-27), Section 1 -- the explicit
# outcome vocabulary services/scheduler.py writes to SchedulerRunEvent.
# outcome at stage="entry"/"settlement". Before this, both stages wrote
# the raw CaptureStatus value (CAPTURED/FAILED) straight through, which
# conflated a genuine NO_ACTION decision (nothing to capture -- the AI
# looked at the event and chose not to trade) with a real entry/
# settlement execution failure (budget too small, no usable quote, a
# provider exception): capture_benchmark_entry correctly records
# CaptureStatus.FAILED for a no-legs decision (there genuinely is no
# EntryCaptureAttempt to make), but that is a successful pipeline
# evaluation, never an infrastructure failure, and must never count
# toward a run's items_failed or render as one in Operations. Reuses the
# same decision_snapshot.legs signal services/benchmark_track_record.py
# and services/operations.py::derive_lifecycle_state already use for
# this exact distinction.
#
# stage="decision" keeps decision_pipeline.py's own Outcome vocabulary
# unchanged (created/already_frozen/skipped_ineligible/skipped_not_due/
# skipped_too_late/skipped_no_company/failed) -- that stage never had
# this conflation (see decision_pipeline.py's own docstring: "failed"
# there already means only a genuine generate_decision()/freeze
# exception, never a no-action decision).
OUTCOME_DECISION_PIPELINE_FAILED = "failed"
OUTCOME_DECISION_NO_ACTION = "decision_no_action"
OUTCOME_ENTRY_CAPTURED = "entry_captured"
OUTCOME_ENTRY_FAILED = "entry_failed"
OUTCOME_SETTLEMENT_CAPTURED = "settlement_captured"
OUTCOME_SETTLEMENT_FAILED = "settlement_failed"

# Every real, distinct "a human should look at this" outcome string
# across all three stages -- services/operations.py::get_recent_failures
# matches on this set (not a single literal "failed") so entry/
# settlement failures keep surfacing in the Failure Center under their
# new, more specific names.
FAILURE_OUTCOMES = frozenset(
    {OUTCOME_DECISION_PIPELINE_FAILED, OUTCOME_ENTRY_FAILED, OUTCOME_SETTLEMENT_FAILED}
)


def start_scheduler_run(db: Session, job_id: str) -> SchedulerRun:
    run = SchedulerRun(job_id=job_id, started_at=datetime.now(UTC), status=RUN_STATUS_RUNNING)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def finish_scheduler_run(
    db: Session,
    run: SchedulerRun,
    *,
    status: str,
    items_evaluated: int | None = None,
    items_succeeded: int | None = None,
    items_failed: int | None = None,
    error_summary: str | None = None,
) -> None:
    finished_at = datetime.now(UTC)
    run.finished_at = finished_at
    run.status = status
    run.duration_ms = int((finished_at - run.started_at).total_seconds() * 1000)
    run.items_evaluated = items_evaluated
    run.items_succeeded = items_succeeded
    run.items_failed = items_failed
    run.error_summary = error_summary
    db.commit()


def record_scheduler_run_event(
    db: Session,
    run: SchedulerRun,
    *,
    calendar_event_id: int | None,
    symbol: str,
    stage: str,
    outcome: str,
    reason: str | None,
) -> None:
    db.add(
        SchedulerRunEvent(
            scheduler_run_id=run.id,
            earnings_calendar_event_id=calendar_event_id,
            symbol=symbol,
            stage=stage,
            outcome=outcome,
            reason=reason,
            occurred_at=datetime.now(UTC),
        )
    )
    db.commit()
