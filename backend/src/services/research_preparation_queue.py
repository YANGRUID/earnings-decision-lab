"""Pre-live hardening (2026-08-25) -- durable claiming, leasing, and
stale-job recovery for the ResearchPreparationJob queue (see that
model's own module docstring for why this table, not a new one, is the
queue). Used exclusively by workers/research_preparation_worker.py --
never by any FastAPI request handler, which is the entire point: a
worker process claims and holds a row for as long as real preparation
work takes, completely independent of any HTTP request's lifetime.

Claiming uses a real ``SELECT ... FOR UPDATE SKIP LOCKED`` (via
SQLAlchemy's ``with_for_update(skip_locked=True)``) so two worker
processes (today there's one; the architecture doesn't assume that
stays true) can never claim the same row -- the database itself
enforces exclusivity, not an in-memory lock or a convention either
worker could get wrong.

Lease/heartbeat, not "trust the process is still alive": a RUNNING row
is only ever treated as truly alive if its heartbeat_at (updated on
every real step transition, see services/research_orchestration.py::
_set_step) is recent. A stale one is real, positive evidence the worker
behind it died -- recover_stale_running_jobs() below is what turns that
into a reclaimable row again, never leaving anything permanently stuck.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent
from models.research_preparation_job import JobStatus, ResearchPreparationJob

log = logging.getLogger("research_preparation_queue")

# Meaningfully longer than any real observed gap between two heartbeats
# (each real step transition -- company identification, historical
# earnings, price history, SEC filings, embeddings, options chain --
# commits one; the slowest single step observed live during this
# project's own pre-live hardening was well under two minutes) so a
# genuinely slow step is never mistaken for a dead worker, while a truly
# abandoned row is still reclaimed with real time left before the next
# scheduled preparation window.
LEASE_TIMEOUT = timedelta(minutes=5)

# Bounds automatic retries -- a company that has genuinely failed this
# many real attempts (each a real network/CPU pipeline run, not a cheap
# retry) stays FAILED rather than being reclaimed forever. Deliberately
# not enforced by claim_next_preparation_job's own WHERE clause for
# PENDING/INTERRUPTED rows below MAX_ATTEMPTS -- see that function's
# own docstring.
MAX_ATTEMPTS = 3

_CLAIMABLE_STATUSES = (JobStatus.PENDING, JobStatus.INTERRUPTED)


@dataclass(frozen=True)
class StaleRecoveryResult:
    recovered_to_interrupted: int
    permanently_failed: int


def recover_stale_running_jobs(
    db: Session, *, now: datetime | None = None, lease_timeout: timedelta = LEASE_TIMEOUT
) -> StaleRecoveryResult:
    """Real, positive detection of a dead worker: a RUNNING row whose
    heartbeat_at is older than ``lease_timeout`` (or, defensively, one
    that somehow never got a heartbeat at all -- should not happen once
    a worker claims it, but treated the same way rather than assumed
    fine). Recovered to INTERRUPTED (reclaimable) if it still has real
    attempts left, or FAILED (permanent, honest) once it's exhausted
    MAX_ATTEMPTS -- either way, this never touches steps already marked
    DONE on the row (see services/research_orchestration.py::
    prepare_company_research's own idempotent, freshness-gated re-run),
    and never deletes anything."""
    now = now or datetime.now(UTC)
    stale_cutoff = now - lease_timeout

    stale_jobs = (
        db.query(ResearchPreparationJob)
        .filter(
            ResearchPreparationJob.status == JobStatus.RUNNING,
            or_(
                ResearchPreparationJob.heartbeat_at.is_(None),
                ResearchPreparationJob.heartbeat_at < stale_cutoff,
            ),
        )
        .all()
    )

    recovered = 0
    failed = 0
    for job in stale_jobs:
        if job.attempt_count >= MAX_ATTEMPTS:
            job.status = JobStatus.FAILED
            job.error = (
                f"worker lease expired after {job.attempt_count} attempts "
                "(heartbeat stopped -- the worker process behind it presumably died)"
            )[:500]
            job.completed_at = now
            failed += 1
        else:
            job.status = JobStatus.INTERRUPTED
            job.error = "worker lease expired (heartbeat stopped) -- reclaimable"[:500]
            recovered += 1
        db.add(job)
        log.warning(
            "research preparation: recovered stale job id=%s ticker=%s -> %s",
            job.id,
            job.ticker,
            job.status.value,
        )
    db.commit()
    return StaleRecoveryResult(recovered_to_interrupted=recovered, permanently_failed=failed)


def claim_next_preparation_job(
    db: Session, worker_id: str, *, now: datetime | None = None
) -> ResearchPreparationJob | None:
    """Atomically claims the single highest-priority claimable row
    (PENDING or INTERRUPTED, attempt_count below MAX_ATTEMPTS), or None
    if nothing is claimable right now. Priority: the linked calendar
    event's own earnings_date, soonest first (Section 12 -- a company
    reporting today must never sit behind one reporting next week); a
    row with no linked event (shouldn't happen for anything this
    function enqueues, but not assumed) sorts last, never first.

    ``FOR UPDATE SKIP LOCKED`` is what makes this safe with more than
    one worker process: a second worker's identical query simply never
    sees a row the first one is mid-claim on, rather than blocking or
    racing to claim the same one.
    """
    now = now or datetime.now(UTC)
    row = (
        db.query(ResearchPreparationJob)
        .outerjoin(
            EarningsCalendarEvent,
            ResearchPreparationJob.earnings_calendar_event_id == EarningsCalendarEvent.id,
        )
        .filter(
            ResearchPreparationJob.status.in_(_CLAIMABLE_STATUSES),
            ResearchPreparationJob.attempt_count < MAX_ATTEMPTS,
        )
        .order_by(EarningsCalendarEvent.earnings_date.asc().nulls_last())
        .with_for_update(of=ResearchPreparationJob, skip_locked=True)
        .first()
    )
    if row is None:
        return None

    row.status = JobStatus.RUNNING
    row.worker_id = worker_id
    row.heartbeat_at = now
    row.attempt_count = row.attempt_count + 1
    row.error = None
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def count_queue_depth(db: Session) -> int:
    """Real, current count of claimable work (PENDING + INTERRUPTED,
    within MAX_ATTEMPTS) -- what Operations shows as "Queue: N pending"."""
    return (
        db.query(ResearchPreparationJob)
        .filter(
            ResearchPreparationJob.status.in_(_CLAIMABLE_STATUSES),
            ResearchPreparationJob.attempt_count < MAX_ATTEMPTS,
        )
        .count()
    )
