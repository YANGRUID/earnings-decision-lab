"""Phase 4.2 -- the first real background scheduler in this codebase.
``apscheduler>=3.10`` has been a declared dependency since early in this
project but was never imported anywhere until now (every "scheduled"
thing before this was either a cron-invoked standalone script, e.g.
ingestion/collect_options_snapshots.py, or FastAPI's own one-shot
BackgroundTasks) -- see PHASE4_ARCHITECTURE_REVIEW.md sec 4 for the full
evaluation of APScheduler vs. Celery vs. cron that led here. Celery is
deliberately not used: it needs a broker and a separate worker process,
real new infrastructure this personal-scale project doesn't otherwise
need. Phase 4.4 sec 14 reaffirms the same choice for decision generation
+ entry capture below -- extended, not replaced.

``AsyncIOScheduler``, started from api/main.py's existing ``lifespan()``
hook, inside the backend process -- the only component docker-compose
actually guarantees is running continuously. ``SQLAlchemyJobStore``
(against the same Postgres instance, its own auto-created
``apscheduler_jobs`` table) so each job's registration survives a
container restart -- "job survives restart if possible" in the Phase 4.2
brief, and "restarting the service must not create duplicate official
entry snapshots" in Phase 4.4 sec 14/15 (the entry-capture job itself is
independently idempotent -- see services/benchmark_entry_capture.py --
so this is a second, real layer of the same guarantee, not the only one).

Each job function opens its own fresh ``SessionLocal()`` (never reuses a
request-scoped session) and is defensive about providers being
unconfigured -- the same pattern ingestion/collect_options_snapshots.py
and this project's other standalone background work already use.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from analytics.decision_timing_policy import V3_TIMING_POLICY, V4_TIMING_POLICY
from analytics.earnings_timing import compute_entry_exit_schedule
from analytics.market_session import EASTERN
from core.config import get_settings

# scheduler_engine/SchedulerSessionLocal, not the API-facing engine/
# SessionLocal (see db/session.py's own docstring for why): every job
# body below, and APScheduler's own SQLAlchemyJobStore, must never
# compete with ordinary API requests for a connection out of the same
# pool -- a real, empirically-observed silent-failure mode this
# separation exists to close.
from db.session import SchedulerSessionLocal as SessionLocal
from db.session import scheduler_engine as engine
from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.enums import CaptureStatus, EarningsCalendarEventStatus
from models.settlement_capture_attempt import SettlementCaptureAttempt
from observability.redact import redact
from providers.factory import build_earnings_calendar_provider, build_options_provider_chain
from providers.ibkr_tws_health import TwsHealthProbe
from rag.embeddings import EmbeddingProvider
from services.benchmark_entry_capture import capture_benchmark_entry
from services.benchmark_exit_capture import (
    _map_timing,  # noqa: PLC2701 -- shared private helper, same BMO/AMC/DMH mapping as the exit service itself
    capture_benchmark_exit,
)
from services.decision_pipeline import (
    _TIMING_TO_ANNOUNCEMENT_TIME,  # noqa: PLC2701 -- shared private mapping, same BMO/AMC/DMH -> AnnouncementTime as the pipeline itself uses
    LATE_CUTOFF_GRACE,
    run_decision_pipeline_for_event,
)
from services.earnings_calendar_sync import sync_earnings_calendar
from services.earnings_research_preparation import EnqueueResult, enqueue_preparation_candidates
from services.llm.factory import get_llm_provider
from services.scheduler_run_tracking import (
    OUTCOME_DECISION_NO_ACTION,
    OUTCOME_ENTRY_CAPTURED,
    OUTCOME_ENTRY_FAILED,
    OUTCOME_SETTLEMENT_CAPTURED,
    OUTCOME_SETTLEMENT_FAILED,
    RUN_STATUS_ERROR,
    RUN_STATUS_SKIPPED,
    RUN_STATUS_SUCCESS,
    finish_scheduler_run,
    record_scheduler_run_event,
    start_scheduler_run,
)
from services.system_status import IbkrStatus, TwsStatus, get_ibkr_status, get_tws_status
from services.v4_shadow_scheduler import (
    V4_SHADOW_DECISION_JOB_ID,
    V4_SHADOW_SETTLEMENT_JOB_ID,
    run_v4_shadow_decision_job,
    run_v4_shadow_settlement_job,
)

# Error summaries are for a human glancing at the Operations page, never
# a full traceback -- capped well short of Text-column-abuse territory,
# and always redacted (see observability/redact.py) before being stored.
_MAX_ERROR_SUMMARY_CHARS = 500


def _error_summary(exc: Exception) -> str:
    return redact(str(exc))[:_MAX_ERROR_SUMMARY_CHARS]


log = logging.getLogger("services.scheduler")

CALENDAR_SYNC_JOB_ID = "earnings_calendar_sync"
EARNINGS_RESEARCH_PREPARATION_JOB_ID = "earnings_research_preparation"
DECISION_AND_ENTRY_CAPTURE_JOB_ID = "decision_and_entry_capture"

EXIT_CAPTURE_JOB_ID = "exit_capture"
IBKR_GATEWAY_HEALTHCHECK_JOB_ID = "ibkr_gateway_healthcheck"

# Pre-live hardening (2026-08-25): shortly after the daily calendar sync
# (00:00 UTC, see CALENDAR_SYNC_JOB_ID's own registration below), giving
# roughly 19 hours of lead time before the real 15:55 ET (19:55 UTC)
# decision/entry window -- "several hours before" with wide margin, per
# the brief this job was built against. Real reason this specific gap
# matters: research preparation makes real SEC EDGAR / market-data /
# options-provider calls, and those must never compete with, or run
# anywhere near, the official window's own real IBKR/LLM work.
_RESEARCH_PREPARATION_HOUR_UTC = 1
_RESEARCH_PREPARATION_MINUTE_UTC = 0

# Phase 4.8A: how often the keep-alive/observability job below polls the
# Gateway, all day -- not just around the two ~15:55 ET capture windows.
# Real reason this needs to exist at all: before this phase, NOTHING
# touched the Gateway between those two daily cron fires (see
# PHASE4_8A_IBKR_RUNTIME_ARCHITECTURE_REVIEW.md sec 3.4/5.3), so an idle-
# timed-out session could go undetected for up to 24 hours, discovered
# only as a FAILED capture attempt. 10 minutes is well inside IBKR's
# documented keep-alive window and cheap (one auth-status call).
IBKR_HEALTHCHECK_INTERVAL_MINUTES = 10

# The real, fixed wall-clock trigger every eligible event's entry_
# timestamp resolves to (analytics/earnings_timing.py::ENTRY_EXIT_TIME)
# -- only the *date* varies per event, never the time, so one daily cron
# job in America/New_York (not the scheduler's default UTC) is
# sufficient; no continuous polling needed. The exit job shares this
# exact same wall-clock time, since ENTRY_EXIT_TIME is also
# compute_entry_exit_schedule()'s exit_timestamp -- see EXIT_CAPTURE_
# JOB_ID's own registration below for why it's still a separate job.
_ENTRY_HOUR_ET = V3_TIMING_POLICY.entry_hour
_ENTRY_MINUTE_ET = V3_TIMING_POLICY.entry_minute

# V4 product consolidation (2026-09-02): the V4 DECISION/entry observation
# moves to 15:30 ET to buy runway before the 16:00 close -- six
# configurations to evaluate, and a late scheduler at 15:55 leaves no room
# to recover.
#
# Deliberately SEPARATE constants. All four jobs previously shared
# _ENTRY_HOUR_ET/_ENTRY_MINUTE_ET, so editing that pair in place would
# have silently moved three things nobody asked to move: the V3 entry
# capture, the V3 exit capture, and the V4 settlement. Entry timing and
# settlement timing are different policies (see analytics/
# decision_timing_policy.py) and only the V4 decision job reads these.
_V4_DECISION_HOUR_ET = V4_TIMING_POLICY.entry_hour
_V4_DECISION_MINUTE_ET = V4_TIMING_POLICY.entry_minute

# Pre-live hardening (2026-08-25): how far forward run_decision_and_
# entry_capture_job's own SQL query looks for candidate events, before
# the exact compute_entry_exit_schedule() timing check below narrows
# further. A real, live-observed problem this exists to fix: without any
# earnings_date bound at all, that query pulled in EVERY real UPCOMING
# event (1500+ in the shared dev DB, most of them months away), and each
# one reached check_eligibility() -- a real IBKR/options-provider call --
# inside run_decision_pipeline_for_event before that function's own
# (unchanged) timing check had a chance to skip it. During the real
# 15:55 ET execution window this meant thousands of unnecessary real
# provider calls competing for time and rate-limit budget against the
# handful of companies actually due that day.
#
# 10 days, not 1: compute_entry_exit_schedule's own decision_date can
# land up to a few real trading days *before* earnings_date (previous_
# trading_day / nearest_trading_day_on_or_before, see analytics/
# earnings_timing.py), so an earnings_date up to ~10 calendar days in
# the future can still resolve to an entry_timestamp of *today* across
# a long holiday weekend -- 10 is a deliberately generous margin over
# the longest realistic NYSE closure stretch, not a tight fit. This is
# only a candidate-set-size pre-filter; the exact due/not-due decision
# for each candidate is still made by the identical, unchanged
# compute_entry_exit_schedule() + LATE_CUTOFF_GRACE window
# run_decision_pipeline_for_event already enforces.
_DECISION_CANDIDATE_LOOKAHEAD_DAYS = 10


def _due_for_decision_now(event: EarningsCalendarEvent, now: datetime) -> bool:
    """True only for an event whose *official* decision/entry window
    (compute_entry_exit_schedule()'s own entry_timestamp, +LATE_CUTOFF_
    GRACE -- both reused unchanged from services.decision_pipeline, never
    redefined here) genuinely intersects ``now``. Orchestration-only: this
    never decides eligibility, strategy, probability, or pricing -- it
    only decides which events are even worth handing to the real
    pipeline during this run, exactly mirroring that pipeline's own
    (unchanged) skipped_not_due/skipped_too_late boundaries so this
    pre-filter can never disagree with it and skip something the
    pipeline itself would have processed."""
    session = _TIMING_TO_ANNOUNCEMENT_TIME[event.earnings_time]
    schedule = compute_entry_exit_schedule(event.earnings_date, session)
    return schedule.entry_timestamp <= now <= schedule.entry_timestamp + LATE_CUTOFF_GRACE


# NOT passed as a job argument on purpose: SQLAlchemyJobStore pickles a
# job's full spec (id, trigger, func, args) to persist it in
# apscheduler_jobs, and FastEmbedProvider wraps an onnxruntime
# InferenceSession, which is not picklable -- passing it via ``args``
# broke scheduler.start() entirely (confirmed: the whole scheduler,
# including the unrelated calendar-sync job, silently failed to start).
# A plain module-level reference, set once by build_scheduler() and read
# fresh by the job function at call time, never gets pickled at all.
_shared_embedder: EmbeddingProvider | None = None

# IBKR TWS Migration -- Phase 3 readiness prep. Same real reason as
# _shared_embedder above: a TWSConnectionManager wraps a live socket
# (ibapi's EClient/EReader internals), not picklable, so it cannot be a
# job argument either -- set once by build_scheduler(), read fresh by
# run_ibkr_gateway_healthcheck_job() at call time. None whenever
# ibkr_provider != "tws" (the real, current default) -- the healthcheck
# job's own Web-only behavior is completely unaffected until this is
# actually populated, which only happens post-cutover.
_shared_tws_health_probe: TwsHealthProbe | None = None

# Phase 4.9 -- APScheduler's own Job object exposes next_run_time
# natively, but not "when did this last actually run" -- tracked here via
# a listener instead (see _record_job_execution/build_scheduler below).
# Deliberately in-memory, not a new DB table: this project already
# accepts "not observed since this process started" as an honest reset
# on restart for comparable live-state facts (e.g. IBKR auth status is
# never persisted either, see services/system_status.py) rather than
# building new persistence for a status-page-only concern. Reset to
# empty by build_scheduler() itself so tests/multiple app instances in
# the same process never see a stale run from an earlier instance.
_last_run_at: dict[str, datetime] = {}
_last_run_status: dict[str, str] = {}


def _record_job_execution(event: JobExecutionEvent) -> None:
    _last_run_at[event.job_id] = datetime.now(UTC)
    _last_run_status[event.job_id] = "error" if event.exception else "success"


@dataclass(frozen=True)
class SchedulerJobStatus:
    job_id: str
    next_run_time: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None


@dataclass(frozen=True)
class SchedulerStatus:
    running: bool
    jobs: list[SchedulerJobStatus]


def get_scheduler_status(scheduler: AsyncIOScheduler | None) -> SchedulerStatus:
    """Real, live introspection of the actual running scheduler -- never
    assumed. ``scheduler`` is None when startup failed (see api/main.py's
    own lifespan() try/except), reported honestly as running=False with
    no jobs, not an error."""
    if scheduler is None:
        return SchedulerStatus(running=False, jobs=[])
    jobs = [
        SchedulerJobStatus(
            job_id=job.id,
            next_run_time=job.next_run_time,
            last_run_at=_last_run_at.get(job.id),
            last_run_status=_last_run_status.get(job.id),
        )
        for job in scheduler.get_jobs()
    ]
    return SchedulerStatus(running=scheduler.running, jobs=jobs)


def run_earnings_calendar_sync_job(from_date: date | None = None) -> None:
    """The actual job body -- registered under a fixed id
    (CALENDAR_SYNC_JOB_ID, ``replace_existing=True``) so a restart that
    re-registers it never ends up with two competing schedules. No
    provider configured is logged and skipped, not a crash -- an
    unconfigured deployment shouldn't take the scheduler itself down.

    ``from_date``: the scheduled cron trigger never passes this (stays
    exactly today-forward); it exists so an on-demand admin call
    (api/routers/admin.py) can widen the sync backward -- see
    services/earnings_calendar_sync.py::sync_earnings_calendar's own
    docstring for why the forward end is never affected by it.
    """
    db = SessionLocal()
    run = start_scheduler_run(db, CALENDAR_SYNC_JOB_ID)
    try:
        settings = get_settings()
        provider = build_earnings_calendar_provider(settings, db)
        if provider is None:
            log.warning(
                "earnings calendar sync skipped: no provider configured "
                "(set EARNINGSAPI_API_KEY or FINNHUB_API_KEY)"
            )
            finish_scheduler_run(
                db, run, status=RUN_STATUS_SKIPPED, error_summary="no provider configured"
            )
            return
        result = sync_earnings_calendar(db, provider, from_date=from_date)
        db.commit()
        actual_provider = getattr(provider, "last_actual_provider", None)
        log.info(
            "earnings calendar sync job complete: provider=%s events_fetched=%d "
            "created=%d updated=%d stale_marked=%d synced_at=%s",
            actual_provider or "unknown",
            result.fetched,
            result.created,
            result.updated,
            result.stale_marked,
            datetime.now(UTC).isoformat(),
        )
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_SUCCESS,
            items_evaluated=result.fetched,
            items_succeeded=result.created + result.updated + result.unchanged,
            items_failed=len(result.profile_fetch_failures),
        )
    except Exception as exc:
        db.rollback()
        log.error("earnings calendar sync job failed", exc_info=True)
        finish_scheduler_run(db, run, status=RUN_STATUS_ERROR, error_summary=_error_summary(exc))
    finally:
        db.close()


def run_earnings_research_preparation_job(*, now: datetime | None = None) -> list[EnqueueResult]:
    """Pre-live hardening (2026-08-25): enqueues durable ResearchPreparation
    Job rows (see services/earnings_research_preparation.py::
    enqueue_preparation_candidates) for upcoming eligible calendar events,
    so run_decision_and_entry_capture_job's own ``skipped_no_company``
    outcome stops depending on someone having manually searched a ticker
    on the Search page first.

    Deliberately scheduled well before the 15:55 ET decision/entry
    window (see _RESEARCH_PREPARATION_HOUR_UTC/_MINUTE_UTC above).
    Enqueueing is fast and synchronous (a handful of cheap DB reads plus
    one lightweight options-chain check per candidate) -- the real
    network/CPU-heavy preparation work happens later, out-of-process, in
    the dedicated research-worker (workers/research_preparation_worker.py),
    never inside this job's own run. This is a deliberate architecture
    change (2026-08-25): this job used to run the actual preparation
    pipeline synchronously in-process; real, live evidence during this
    project's own pre-live hardening showed that pattern is not
    restart-resilient (a container restart mid-run abandons whatever
    company was in flight, leaving a permanent zombie RUNNING row with no
    automatic recovery) -- see services/research_preparation_queue.py for
    the claim/lease/heartbeat/recovery semantics that now own that
    resilience instead. This job never generates a decision, never
    freezes a DecisionSnapshot, never captures an option entry price.

    ``now``: the real cron trigger never passes this; exists purely for
    tests, mirroring every other job here.

    Returns the real per-candidate ``EnqueueResult`` list -- APScheduler's
    own cron trigger discards it (every sibling job here returns None to
    it), but api/routers/admin.py's on-demand endpoint reuses it directly
    so there is exactly one implementation of "what does this job do",
    never a second, parallel admin-only version (see that router's own
    module docstring).
    """
    db = SessionLocal()
    run = start_scheduler_run(db, EARNINGS_RESEARCH_PREPARATION_JOB_ID)
    try:
        settings = get_settings()
        options_provider = build_options_provider_chain(settings, db=db)
        results = enqueue_preparation_candidates(db, options_provider, now=now)
        for result in results:
            record_scheduler_run_event(
                db,
                run,
                calendar_event_id=result.calendar_event_id,
                symbol=result.symbol,
                stage="preparation",
                outcome=result.outcome,
                reason=result.reason,
            )
        # Enqueueing itself either succeeds for a candidate (queued /
        # already_ready / filtered_out are all real, non-error outcomes)
        # or the whole job raises -- there is no per-candidate "failed"
        # outcome here, unlike the actual preparation work the worker
        # does later.
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_SUCCESS,
            items_evaluated=len(results),
            items_succeeded=len(results),
            items_failed=0,
        )
        return results
    except Exception as exc:
        db.rollback()
        log.error("earnings research preparation job failed", exc_info=True)
        finish_scheduler_run(db, run, status=RUN_STATUS_ERROR, error_summary=_error_summary(exc))
        return []
    finally:
        db.close()


def run_decision_and_entry_capture_job(*, now: datetime | None = None) -> None:
    """Phase 4.4 sec 14: for every UPCOMING calendar event, runs the
    Phase 4.3 decision-freezing pipeline and, immediately after (same job
    run, same real-time window -- deliberately not two separate jobs, so
    a frozen decision's entry price is never captured meaningfully later
    than its own generation), attempts the official entry capture. Both
    steps are independently idempotent (a decision or entry already
    captured is a cheap read, never a second generation/capture) -- a
    restart mid-run just re-checks and continues, never duplicates.

    Reads ``_shared_embedder`` (the app's single instance, loaded once at
    startup -- see api/main.py::lifespan and build_scheduler() below)
    fresh at call time rather than taking it as a job argument: this
    job's spec gets pickled by SQLAlchemyJobStore to persist it, and
    FastEmbedProvider (wrapping an onnxruntime InferenceSession) is not
    picklable -- passing it as ``args`` broke scheduler.start() entirely.
    A missing embedder (startup failed to load it) is logged and
    skipped, not a crash.

    ``now``: the real cron trigger never passes this (stays exactly
    real wall-clock time, matching every other job here); it exists
    purely so a test can fix "now" to a specific due window without
    monkeypatching the datetime module, mirroring run_decision_
    pipeline_for_event's own identical ``now`` parameter.
    """
    embedder = _shared_embedder
    if embedder is None:
        log.warning("decision/entry capture run skipped: no embedding model available")
        return

    db = SessionLocal()
    run = start_scheduler_run(db, DECISION_AND_ENTRY_CAPTURE_JOB_ID)
    items_evaluated = 0
    items_failed = 0
    try:
        settings = get_settings()
        llm = get_llm_provider(settings, db=db)
        options_provider = build_options_provider_chain(settings, db=db)

        portfolio = (
            db.query(BenchmarkPortfolio)
            .filter_by(is_active=True)
            .order_by(BenchmarkPortfolio.id)
            .first()
        )
        if portfolio is None:
            log.warning("decision/entry capture run skipped: no active benchmark_portfolio")
            finish_scheduler_run(
                db, run, status=RUN_STATUS_SKIPPED, error_summary="no active benchmark_portfolio"
            )
            return

        now = now or datetime.now(UTC)
        today_et = now.astimezone(EASTERN).date()
        candidate_events = (
            db.query(EarningsCalendarEvent)
            .filter(
                EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING,
                EarningsCalendarEvent.earnings_date >= today_et,
                EarningsCalendarEvent.earnings_date
                <= today_et + timedelta(days=_DECISION_CANDIDATE_LOOKAHEAD_DAYS),
            )
            .all()
        )
        events = [event for event in candidate_events if _due_for_decision_now(event, now)]
        for event in events:
            items_evaluated += 1
            try:
                outcome = run_decision_pipeline_for_event(
                    db, event, portfolio, options_provider, llm, embedder, now=now
                )
                db.commit()
            except Exception as exc:
                db.rollback()
                log.error(
                    "decision pipeline failed for calendar_event_id=%s", event.id, exc_info=True
                )
                items_failed += 1
                record_scheduler_run_event(
                    db,
                    run,
                    calendar_event_id=event.id,
                    symbol=event.symbol,
                    stage="decision",
                    outcome="failed",
                    reason=_error_summary(exc),
                )
                continue

            record_scheduler_run_event(
                db,
                run,
                calendar_event_id=event.id,
                symbol=event.symbol,
                stage="decision",
                outcome=outcome.outcome,
                reason=outcome.reason,
            )
            if outcome.outcome == "failed":
                items_failed += 1

            if outcome.decision_snapshot_id is None:
                continue
            try:
                decision_snapshot = db.get(DecisionSnapshot, outcome.decision_snapshot_id)
                if decision_snapshot is not None and options_provider is not None:
                    attempt = capture_benchmark_entry(
                        db,
                        decision_snapshot=decision_snapshot,
                        portfolio=portfolio,
                        options_provider=options_provider,
                        now=now,
                    )
                    db.commit()
                    # Post-official-run cleanup (2026-08-27), Section 1 --
                    # see scheduler_run_tracking.py's own docstring on
                    # these constants for the full rationale: a no-legs
                    # decision's FAILED EntryCaptureAttempt is a real,
                    # successful pipeline evaluation, never a failure.
                    if attempt.status == CaptureStatus.CAPTURED:
                        entry_outcome = OUTCOME_ENTRY_CAPTURED
                    elif not decision_snapshot.legs:
                        entry_outcome = OUTCOME_DECISION_NO_ACTION
                    else:
                        entry_outcome = OUTCOME_ENTRY_FAILED
                        items_failed += 1
                    record_scheduler_run_event(
                        db,
                        run,
                        calendar_event_id=event.id,
                        symbol=event.symbol,
                        stage="entry",
                        outcome=entry_outcome,
                        reason=attempt.capture_error,
                    )
            except Exception as exc:
                db.rollback()
                log.error(
                    "entry capture failed for decision_snapshot_id=%s",
                    outcome.decision_snapshot_id,
                    exc_info=True,
                )
                items_failed += 1
                record_scheduler_run_event(
                    db,
                    run,
                    calendar_event_id=event.id,
                    symbol=event.symbol,
                    stage="entry",
                    outcome=OUTCOME_ENTRY_FAILED,
                    reason=_error_summary(exc),
                )
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_SUCCESS,
            items_evaluated=items_evaluated,
            items_succeeded=items_evaluated - items_failed,
            items_failed=items_failed,
        )
    except Exception as exc:
        # Setup failure (LLM/options-provider construction, the initial
        # portfolio/events queries) -- distinct from a single event's own
        # try/except above, which already handles per-event failures
        # without ever reaching here. Recorded, then re-raised: the
        # original, pre-instrumentation behavior let such an exception
        # propagate uncaught to APScheduler, whose own EVENT_JOB_ERROR
        # listener (_record_job_execution) is what get_scheduler_status()
        # already reports through -- swallowing it here instead would
        # make that existing status silently start lying (a real setup
        # failure would read back as a clean "success").
        db.rollback()
        log.error("decision/entry capture job failed", exc_info=True)
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_ERROR,
            items_evaluated=items_evaluated,
            items_failed=items_failed,
            error_summary=_error_summary(exc),
        )
        raise
    finally:
        db.close()


def run_exit_capture_job() -> None:
    """Phase 4.5 approved decision 5: a job of its own, not folded into
    run_decision_and_entry_capture_job -- the two scan disjoint decision
    sets ("just entered, nothing to exit yet" vs. "already entered, due
    for exit"), so a missed or slow entry run should never block or
    delay the unrelated exit run for a different day's decisions.

    Scans every decision with a real CAPTURED entry but no CAPTURED
    settlement yet, computes each one's real exit_date via
    compute_entry_exit_schedule() (never a naive "T+1 from entry"
    assumption -- BMO and AMC resolve differently, see analytics/
    earnings_timing.py), and only attempts capture_benchmark_exit for
    the ones actually due today. This pre-filter (rather than calling
    capture_benchmark_exit for every entered-but-unsettled decision
    every day) matters: without it, every decision not yet due for exit
    would grow a spurious FAILED settlement attempt row on every single
    day it isn't due, since capture_benchmark_exit's own no-lookahead
    check would reject it anyway -- honest, but noisy and wasteful.

    Needs no embedder/LLM at all -- settlement never calls generate_
    decision() or any AI path, only real market data via the options
    provider.
    """
    db = SessionLocal()
    run = start_scheduler_run(db, EXIT_CAPTURE_JOB_ID)
    items_evaluated = 0
    items_failed = 0
    try:
        settings = get_settings()
        options_provider = build_options_provider_chain(settings, db=db)
        if options_provider is None:
            log.warning("exit capture run skipped: no options provider configured")
            finish_scheduler_run(
                db, run, status=RUN_STATUS_SKIPPED, error_summary="no options provider configured"
            )
            return

        portfolio = (
            db.query(BenchmarkPortfolio)
            .filter_by(is_active=True)
            .order_by(BenchmarkPortfolio.id)
            .first()
        )
        if portfolio is None:
            log.warning("exit capture run skipped: no active benchmark_portfolio")
            finish_scheduler_run(
                db, run, status=RUN_STATUS_SKIPPED, error_summary="no active benchmark_portfolio"
            )
            return

        now = datetime.now(UTC)
        today_et = now.astimezone(EASTERN).date()

        entered_ids = db.query(EntryCaptureAttempt.decision_snapshot_id).filter(
            EntryCaptureAttempt.benchmark_portfolio_id == portfolio.id,
            EntryCaptureAttempt.status == CaptureStatus.CAPTURED,
        )
        settled_ids = db.query(SettlementCaptureAttempt.decision_snapshot_id).filter(
            SettlementCaptureAttempt.benchmark_portfolio_id == portfolio.id,
            SettlementCaptureAttempt.status == CaptureStatus.CAPTURED,
        )
        candidates = (
            db.query(DecisionSnapshot)
            .filter(DecisionSnapshot.id.in_(entered_ids))
            .filter(DecisionSnapshot.id.notin_(settled_ids))
            .all()
        )

        for decision_snapshot in candidates:
            calendar_event = decision_snapshot.earnings_calendar_event
            schedule = compute_entry_exit_schedule(
                calendar_event.earnings_date, _map_timing(calendar_event.earnings_time)
            )
            if schedule.exit_date != today_et:
                continue
            items_evaluated += 1
            try:
                attempt = capture_benchmark_exit(
                    db,
                    decision_snapshot=decision_snapshot,
                    portfolio=portfolio,
                    options_provider=options_provider,
                    now=now,
                )
                db.commit()
                # Post-official-run cleanup (2026-08-27), Section 1 --
                # same clean vocabulary as the entry stage, for the same
                # cross-surface consistency; settlement has no NO_ACTION
                # equivalent (a candidate here was already a captured
                # entry, so it always has real legs to close).
                settlement_outcome = (
                    OUTCOME_SETTLEMENT_CAPTURED
                    if attempt.status == CaptureStatus.CAPTURED
                    else OUTCOME_SETTLEMENT_FAILED
                )
                record_scheduler_run_event(
                    db,
                    run,
                    calendar_event_id=calendar_event.id,
                    symbol=calendar_event.symbol,
                    stage="settlement",
                    outcome=settlement_outcome,
                    reason=attempt.capture_error,
                )
                if attempt.status == CaptureStatus.FAILED:
                    items_failed += 1
            except Exception as exc:
                db.rollback()
                log.error(
                    "exit capture failed for decision_snapshot_id=%s",
                    decision_snapshot.id,
                    exc_info=True,
                )
                items_failed += 1
                record_scheduler_run_event(
                    db,
                    run,
                    calendar_event_id=calendar_event.id,
                    symbol=calendar_event.symbol,
                    stage="settlement",
                    outcome=OUTCOME_SETTLEMENT_FAILED,
                    reason=_error_summary(exc),
                )
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_SUCCESS,
            items_evaluated=items_evaluated,
            items_succeeded=items_evaluated - items_failed,
            items_failed=items_failed,
        )
    except Exception as exc:
        # Same rationale as run_decision_and_entry_capture_job's own
        # outer handler: record, then re-raise, so a setup failure still
        # reaches APScheduler's real EVENT_JOB_ERROR listener exactly as
        # it did before this instrumentation existed.
        db.rollback()
        log.error("exit capture job failed", exc_info=True)
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_ERROR,
            items_evaluated=items_evaluated,
            items_failed=items_failed,
            error_summary=_error_summary(exc),
        )
        raise
    finally:
        db.close()


def run_ibkr_gateway_healthcheck_job() -> None:
    """Phase 4.8A -- periodic, low-cost observation of the active IBKR
    provider's real health, independent of the two daily capture jobs.
    Two real purposes (see PHASE4_8A_IBKR_RUNTIME_ARCHITECTURE_REVIEW.md
    sec 3.4/5.3): (1) a keep-alive touch well inside IBKR's session-idle
    window, since nothing else calls the Gateway between the two ~15:55
    ET cron fires; (2) a structured, greppable log line every run gives
    an operator real visibility into reconnect/re-auth cycles well
    before the next actual capture window, instead of only discovering a
    dead session from a FAILED capture attempt at 15:55 ET.

    IBKR TWS Migration, Phase 3 readiness -- provider-aware (real Phase-3
    blocker fixed here): this used to hardcode the Web-only auth check
    (services.system_status.get_ibkr_status(), the Client Portal
    Gateway's own /iserver/auth/status) regardless of which transport was
    actually selected -- wrong the moment ibkr_provider becomes "tws".
    Branches on the real, current setting: WEB keeps the exact original
    behavior (get_ibkr_status, unchanged, still what the Settings ->
    Interactive Brokers page itself checks); TWS reads
    services.system_status.get_tws_status() against the SAME shared,
    long-lived TwsHealthProbe api/main.py's lifespan() owns for
    /system-status (see _shared_tws_health_probe below) -- never a fresh
    connect/disconnect per job run, and never a second, independently-
    drifting notion of "connected" from the status page's own.

    Deliberately attempts no remediation itself for either transport: no
    call to the Web Gateway automation container's own activation
    endpoints, and no forced TWS reconnect beyond what the shared probe
    already does on its own bounded backoff. Session recovery is that
    automation layer's own job (IBeam already restarts the Gateway
    process internally on a dropped Web session); a dropped TWS session
    requires the same real, manual re-authentication in IB Gateway's own
    window this whole migration has never attempted to automate. Both
    status calls are pure, no DB read/write of their own; the only DB
    access this job makes is the Operations Monitor's own scheduler_run
    tracking (see services/scheduler_run_tracking.py).

    Skips entirely, logged, when OPTIONS_PROVIDER isn't ibkr -- a
    deployment that has never opted into IBKR shouldn't get a recurring
    "gateway unreachable" warning for a Gateway it never configured,
    mirroring run_earnings_calendar_sync_job's own precedent of skipping
    cleanly when its own precondition isn't configured.
    """
    db = SessionLocal()
    run = start_scheduler_run(db, IBKR_GATEWAY_HEALTHCHECK_JOB_ID)
    try:
        settings = get_settings()
        if settings.options_provider.lower() != "ibkr":
            log.debug("ibkr gateway healthcheck skipped: OPTIONS_PROVIDER is not ibkr")
            finish_scheduler_run(
                db, run, status=RUN_STATUS_SKIPPED, error_summary="OPTIONS_PROVIDER is not ibkr"
            )
            return
        transport = settings.ibkr_provider.lower()
        status: IbkrStatus | TwsStatus
        if transport == "tws":
            status = get_tws_status(settings, probe=_shared_tws_health_probe)
        else:
            status = get_ibkr_status(settings)
        if status.status_label == "CONNECTED":
            log.info("ibkr gateway healthcheck (%s): %s", transport, status.status_label)
            finish_scheduler_run(db, run, status=RUN_STATUS_SUCCESS)
        else:
            log.warning(
                "ibkr gateway healthcheck (%s): %s%s",
                transport,
                status.status_label,
                f" ({status.error})" if status.error else "",
            )
            summary = (
                f"{status.status_label}: {redact(status.error)}"
                if status.error
                else status.status_label
            )
            finish_scheduler_run(
                db,
                run,
                status=RUN_STATUS_ERROR,
                error_summary=summary[:_MAX_ERROR_SUMMARY_CHARS],
            )
    except Exception as exc:
        log.error("ibkr gateway healthcheck job failed", exc_info=True)
        finish_scheduler_run(db, run, status=RUN_STATUS_ERROR, error_summary=_error_summary(exc))
    finally:
        db.close()




def build_scheduler(
    embedder: EmbeddingProvider | None = None,
    tws_health_probe: TwsHealthProbe | None = None,
) -> AsyncIOScheduler:
    """Constructs (but does not start) the scheduler -- see
    api/main.py::lifespan for the actual start/shutdown lifecycle.
    ``embedder`` is stashed on the module-level ``_shared_embedder``
    (never passed as a job argument -- see run_decision_and_entry_
    capture_job's own docstring for why that broke job persistence).
    ``tws_health_probe`` is stashed the same way, on ``_shared_tws_
    health_probe`` -- same non-picklable-object constraint, and (see
    run_ibkr_gateway_healthcheck_job's own docstring) the same shared
    instance api/main.py::lifespan already owns for /system-status, so
    the healthcheck job and the status page can never independently
    drift on what "connected" means for TWS either. None whenever the
    caller never built one (ibkr_provider != "tws", the current, real
    default) -- the healthcheck job's WEB branch is entirely unaffected."""
    global _shared_embedder, _shared_tws_health_probe
    _shared_embedder = embedder
    _shared_tws_health_probe = tws_health_probe
    # Fresh per build_scheduler() call -- a new app instance (e.g. a
    # second TestClient app in the same test process, see test_services_
    # scheduler.py) must not see stale "last run" data from an earlier
    # instance's jobs.
    _last_run_at.clear()
    _last_run_status.clear()

    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=engine)},
        timezone="UTC",
    )
    scheduler.add_listener(_record_job_execution, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.add_job(
        run_earnings_calendar_sync_job,
        trigger="cron",
        hour=0,
        minute=0,
        id=CALENDAR_SYNC_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_earnings_research_preparation_job,
        trigger="cron",
        hour=_RESEARCH_PREPARATION_HOUR_UTC,
        minute=_RESEARCH_PREPARATION_MINUTE_UTC,
        id=EARNINGS_RESEARCH_PREPARATION_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_decision_and_entry_capture_job,
        trigger="cron",
        hour=_ENTRY_HOUR_ET,
        minute=_ENTRY_MINUTE_ET,
        timezone="America/New_York",
        id=DECISION_AND_ENTRY_CAPTURE_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_exit_capture_job,
        trigger="cron",
        hour=_ENTRY_HOUR_ET,
        minute=_ENTRY_MINUTE_ET,
        timezone="America/New_York",
        id=EXIT_CAPTURE_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_ibkr_gateway_healthcheck_job,
        trigger="interval",
        minutes=IBKR_HEALTHCHECK_INTERVAL_MINUTES,
        id=IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
        replace_existing=True,
    )

    # V4.5 (Sections 33/34/99) -- shadow jobs are registered ONLY when the
    # activation flag is on. With it off (the default and the current
    # production state) they do not exist in the job store at all, so
    # there is nothing to fire accidentally and nothing for Operations to
    # misreport as an active-but-failing job.
    #
    # Registered LAST, and inside its own try/except, for a real reason
    # found by this project's own V4-isolation test: api/main.py wraps
    # build_scheduler() in a try/except that disables the ENTIRE scheduler
    # on failure. Without this guard, an exception while registering an
    # EXPERIMENTAL V4 job would take every OFFICIAL V3 job down with it --
    # exactly the "V4 must never block V3" rule inverted. Official jobs are
    # already registered above by the time this runs, so a shadow failure
    # here costs only the shadow cohort.
    try:
        if get_settings().v4_shadow_enabled:
            scheduler.add_job(
                run_v4_shadow_decision_job,
                trigger="cron",
                hour=_V4_DECISION_HOUR_ET,
                minute=_V4_DECISION_MINUTE_ET,
                timezone="America/New_York",
                id=V4_SHADOW_DECISION_JOB_ID,
                replace_existing=True,
            )
            # Settlement stays at 15:55 ET on purpose -- the T+1 exit
            # benchmark is unchanged from V3. Only ENTRY timing moved.
            scheduler.add_job(
                run_v4_shadow_settlement_job,
                trigger="cron",
                hour=_ENTRY_HOUR_ET,
                minute=_ENTRY_MINUTE_ET,
                timezone="America/New_York",
                id=V4_SHADOW_SETTLEMENT_JOB_ID,
                replace_existing=True,
            )
    except Exception:  # noqa: BLE001 -- V4 must never break V3's scheduler
        log.error(
            "V4 shadow job registration failed; official V3 jobs remain registered",
            exc_info=True,
        )
    return scheduler
