"""Pre-live hardening (2026-08-25) -- automatic research preparation for
upcoming eligible earnings calendar events, so the official 15:55 ET
decision/entry job never returns ``skipped_no_company`` for a genuinely
eligible candidate just because nobody happened to search it on the
Search page first.

This module owns exactly one job: deciding which real calendar events
are worth researching at all (the cheap filter -- market cap, US
listing, then a real but lightweight options-chain check, reusing
``services.earnings_eligibility.check_eligibility``, the exact same
gate the official decision pipeline itself uses) and enqueueing a
durable ``ResearchPreparationJob`` row for each one that survives it.

It never does the actual (network/CPU-heavy) preparation work -- that
is the dedicated research-worker's job (workers/research_preparation_
worker.py via services/research_preparation_queue.py's claim/lease/
heartbeat), running in its own process, never inside this call's own
lifetime. This is a deliberate architecture change (2026-08-25): this
module used to also run ``prepare_company_research`` synchronously,
in-process, for every surviving candidate -- real, live evidence during
this project's own pre-live hardening showed that pattern is not
restart-resilient (a container restart mid-run abandons whatever
company was in flight, leaving a permanent zombie RUNNING row with no
automatic recovery). Enqueueing is instant and durable; the worker
that later claims and processes each row is what's actually resilient.

Never creates or touches a DecisionSnapshot, EntryCaptureAttempt, or
SettlementCaptureAttempt, never calls generate_decision() or
freeze_decision_snapshot(), and never captures an option entry price --
this is real, legitimately-available-now information only (company
metadata, already-public SEC filings, historical prices, historical
earnings, deterministic analytics). The official decision still only
happens at the existing legal decision/entry window
(services/decision_pipeline.py, unchanged).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus
from models.research_preparation_job import JobStatus, ResearchPreparationJob
from providers.base import OptionsDataProvider
from services.earnings_eligibility import check_eligibility

# How far ahead of "today" the preparation scan looks. Deliberately wider
# than run_decision_and_entry_capture_job's own _DECISION_CANDIDATE_
# LOOKAHEAD_DAYS (services/scheduler.py) -- that window exists to bound
# what the 15:55 ET job itself considers *due right now*; this one exists
# to give a genuinely new company several real days of lead time to be
# researched *before* its own due day arrives, not to mirror that job's
# window. Still bounded, for the same reason that one is: never scan
# months of future calendar rows for real provider work.
PREPARATION_LOOKAHEAD_DAYS = 5

# A prior row in any of these states means "don't enqueue a duplicate" --
# it's either already in the queue, already being worked, or already
# genuinely done. A FAILED row is deliberately NOT in this set: a
# transient failure (e.g. a rate-limited provider) shouldn't permanently
# block a real candidate from ever being retried on a later scan.
_NO_REENQUEUE_STATUSES = (
    JobStatus.PENDING,
    JobStatus.RUNNING,
    JobStatus.INTERRUPTED,
    JobStatus.COMPLETED,
    JobStatus.COMPLETED_WITH_WARNINGS,
)
_READY_JOB_STATUSES = (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS)

EnqueueOutcome = Literal["queued", "already_ready", "filtered_out", "preparation_warning"]


@dataclass(frozen=True)
class EnqueueResult:
    calendar_event_id: int
    symbol: str
    outcome: EnqueueOutcome
    reason: str | None


def candidate_events_for_preparation(
    db: Session, *, now: datetime | None = None, lookahead_days: int = PREPARATION_LOOKAHEAD_DAYS
) -> list[EarningsCalendarEvent]:
    """Every real UPCOMING event whose earnings_date falls in the
    preparation window -- a plain calendar-date bound, not a due-window
    computation (compute_entry_exit_schedule is never called here; that
    stays exclusively services/scheduler.py's own concern for the
    official decision job)."""
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    return (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING,
            EarningsCalendarEvent.earnings_date >= today,
            EarningsCalendarEvent.earnings_date <= today + timedelta(days=lookahead_days),
        )
        .order_by(EarningsCalendarEvent.market_cap.desc().nullslast())
        .all()
    )


def _latest_job_for_event(
    db: Session, ticker: str, calendar_event_id: int
) -> ResearchPreparationJob | None:
    return (
        db.query(ResearchPreparationJob)
        .filter(
            ResearchPreparationJob.ticker == ticker,
            ResearchPreparationJob.earnings_calendar_event_id == calendar_event_id,
        )
        .order_by(ResearchPreparationJob.id.desc())
        .first()
    )


def v4_research_ready(db: Session, symbol: str, *, now: datetime) -> tuple[bool, str]:
    """The V4 decision gate's own readiness definition (V4-only reset,
    2026-09-02): a Company row exists AND a fresh AI thesis is on record.
    Shared with the catch-up pass so "ready" means the same thing at 13:00
    ET as it does at 15:30 ET."""
    from models.ai_thesis_version import AIThesisVersion  # noqa: PLC0415
    from models.company import Company  # noqa: PLC0415
    from services.research_orchestration import THESIS_FRESHNESS_DAYS  # noqa: PLC0415

    company = db.query(Company).filter_by(ticker=symbol).one_or_none()
    if company is None:
        return False, "no Company row"
    latest = (
        db.query(AIThesisVersion)
        .filter_by(company_id=company.id)
        .order_by(AIThesisVersion.created_at.desc())
        .first()
    )
    if latest is None:
        return False, "no AI thesis"
    if (now - latest.created_at).total_seconds() >= THESIS_FRESHNESS_DAYS * 86400:
        return False, f"AI thesis is {(now - latest.created_at).days}d old"
    return True, ""


def enqueue_readiness_catchup(
    db: Session,
    options_provider: OptionsDataProvider | None,
    *,
    now: datetime | None = None,
    lookahead_days: int = 3,
) -> list[EnqueueResult]:
    """Same-day / startup catch-up: the nightly enqueue for a shorter
    horizon, with the V4 readiness rule applied to already-prepared
    companies (a COMPLETED job without a fresh thesis is queued again --
    the worker's data steps are freshness-gated, only the thesis is new
    work)."""
    return enqueue_preparation_candidates(
        db, options_provider, now=now, lookahead_days=lookahead_days
    )


def enqueue_preparation_candidates(
    db: Session,
    options_provider: OptionsDataProvider | None,
    *,
    now: datetime | None = None,
    lookahead_days: int = PREPARATION_LOOKAHEAD_DAYS,
) -> list[EnqueueResult]:
    """Cheap filter first, a durable queue row only for what survives it
    -- exactly Section 4's own diagram: calendar universe -> cheap
    eligibility filter -> research preparation queue. Fast and
    synchronous by design (a handful of cheap DB reads plus, only for
    eligible candidates, one lightweight options-chain call already made
    by check_eligibility) -- safe to call directly from an HTTP request
    or the scheduler without owning any long-running work.

    Idempotent: an event that already has a PENDING/RUNNING/INTERRUPTED
    row, or a real successfully-COMPLETED one, is never enqueued a
    second time -- see _NO_REENQUEUE_STATUSES above.
    """
    now = now or datetime.now(UTC)
    results: list[EnqueueResult] = []

    for event in candidate_events_for_preparation(db, now=now, lookahead_days=lookahead_days):
        eligibility = check_eligibility(event, options_provider)
        if not eligibility.eligible:
            # Post-live correction (2026-08-25): a retryable (transient
            # provider-call) failure is honestly represented as a
            # non-terminal warning, never the same "filtered_out" a real,
            # permanent business-rule rejection gets -- see
            # EligibilityResult.retryable's own docstring for the real
            # Aug 25 evidence (WSM) this distinction exists to fix.
            outcome: EnqueueOutcome = (
                "preparation_warning" if eligibility.retryable else "filtered_out"
            )
            results.append(EnqueueResult(event.id, event.symbol, outcome, eligibility.reason))
            continue

        existing = _latest_job_for_event(db, event.symbol, event.id)
        if existing is not None and existing.status in _NO_REENQUEUE_STATUSES:
            if existing.status in _READY_JOB_STATUSES:
                # V4-only reset (2026-09-02): "prepared" is not "V4-ready".
                # A completed job whose company still lacks a fresh AI
                # thesis (e.g. prepared before the thesis step existed) is
                # queued again so the decision window does not meet
                # RESEARCH_NOT_READY for a company that was, on paper, done.
                ready, why = v4_research_ready(db, event.symbol, now=now)
                if not ready:
                    job = ResearchPreparationJob(
                        ticker=event.symbol,
                        earnings_calendar_event_id=event.id,
                        status=JobStatus.PENDING,
                        steps=[],
                        started_at=now,
                        attempt_count=0,
                    )
                    db.add(job)
                    db.commit()
                    results.append(
                        EnqueueResult(event.id, event.symbol, "queued", f"not V4-ready: {why}")
                    )
                    continue
                results.append(EnqueueResult(event.id, event.symbol, "already_ready", None))
                continue
            results.append(
                EnqueueResult(
                    event.id, event.symbol, "already_ready", f"already {existing.status.value}"
                )
            )
            continue

        job = ResearchPreparationJob(
            ticker=event.symbol,
            earnings_calendar_event_id=event.id,
            status=JobStatus.PENDING,
            steps=[],
            started_at=now,
            attempt_count=0,
        )
        db.add(job)
        db.commit()
        results.append(EnqueueResult(event.id, event.symbol, "queued", None))

    return results


def enqueue_ticker_for_preparation(
    db: Session, ticker: str, *, now: datetime | None = None
) -> ResearchPreparationJob:
    """On-demand counterpart to ``enqueue_preparation_candidates`` above
    (AI Research architecture fix, 2026-08-26) -- a real caller asking
    about a ticker that isn't calendar-driven (AI Research today; the
    Search page's own ``/prepare``/``/refresh`` still use the older
    ``BackgroundTasks`` path unchanged) gets the same durable queue row
    and the same worker (workers/research_preparation_worker.py) as an
    automatically-scheduled candidate, identified only by
    ``earnings_calendar_event_id`` being null -- see that column's own
    docstring. Never runs preparation itself, for the same restart-
    resilience reason ``enqueue_preparation_candidates`` doesn't either.

    Idempotent: reuses an existing PENDING/RUNNING/INTERRUPTED row for
    this ticker -- calendar-driven or not, since a company already queued
    for its upcoming earnings event doesn't need a second, redundant
    on-demand row -- rather than enqueueing a duplicate.
    """
    now = now or datetime.now(UTC)
    existing = (
        db.query(ResearchPreparationJob)
        .filter(
            ResearchPreparationJob.ticker == ticker,
            ResearchPreparationJob.status.in_(
                (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.INTERRUPTED)
            ),
        )
        .order_by(ResearchPreparationJob.id.desc())
        .first()
    )
    if existing is not None:
        return existing

    job = ResearchPreparationJob(
        ticker=ticker,
        earnings_calendar_event_id=None,
        status=JobStatus.PENDING,
        steps=[],
        started_at=now,
        attempt_count=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
