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

from analytics.decision_timing_policy import (
    V4_ACTIVE_TIMING_POLICY,
)
from analytics.forward_windows import LATE_CUTOFF_GRACE
from core.config import get_settings

# scheduler_engine/SchedulerSessionLocal, not the API-facing engine/
# SessionLocal (see db/session.py's own docstring for why): every job
# body below, and APScheduler's own SQLAlchemyJobStore, must never
# compete with ordinary API requests for a connection out of the same
# pool -- a real, empirically-observed silent-failure mode this
# separation exists to close.
from db.session import SchedulerSessionLocal as SessionLocal
from db.session import scheduler_engine as engine
from observability.redact import redact
from providers.factory import build_earnings_calendar_provider, build_options_provider_chain
from providers.ibkr_tws_health import TwsHealthProbe
from rag.embeddings import EmbeddingProvider
from services.earnings_calendar_sync import sync_earnings_calendar
from services.earnings_research_preparation import (
    EnqueueResult,
    enqueue_preparation_candidates,
    enqueue_readiness_catchup,
)
from services.scheduler_run_tracking import (
    RUN_STATUS_ERROR,
    RUN_STATUS_SKIPPED,
    RUN_STATUS_SUCCESS,
    finish_scheduler_run,
    record_scheduler_run_event,
    start_scheduler_run,
)
from services.system_status import IbkrStatus, TwsStatus, get_ibkr_status, get_tws_status
from services.us_listing import default_us_listing
from services.v4_shadow_scheduler import (
    RETIRED_V4_JOB_IDS,
    V4_FORWARD_WINDOW_JOB_ID,
    run_v4_forward_window_job,
)
from services.v4_shadow_scheduler import V4_SHADOW_DECISION_JOB_ID as V4_SHADOW_DECISION_JOB_ID
from services.v4_shadow_scheduler import (
    V4_SHADOW_SETTLEMENT_JOB_ID as V4_SHADOW_SETTLEMENT_JOB_ID,
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
_V4_DECISION_HOUR_ET = V4_ACTIVE_TIMING_POLICY.entry_hour
_V4_DECISION_MINUTE_ET = V4_ACTIVE_TIMING_POLICY.entry_minute
# V4-only reset (2026-09-02): the settlement observation moved to the active
# policy's exit time (15:30 ET, first post-earnings trading day).
_V4_SETTLEMENT_HOUR_ET = V4_ACTIVE_TIMING_POLICY.exit_time.hour
_V4_SETTLEMENT_MINUTE_ET = V4_ACTIVE_TIMING_POLICY.exit_time.minute

# Research readiness catch-up (V4-only reset, 2026-09-02). Evidence: the
# nightly 01:00 UTC preparation cron did not fire on any night between
# 2026-08-25 and 2026-09-01 -- the backend was down or restarting at that
# minute every time, and APScheduler's default 1-second misfire grace
# simply dropped the day's run (Operations kept saying "last run Aug 25").
# Three defences, all idempotent enqueues (the worker does the real work):
#   * a generous misfire grace on the nightly crons, so a run delayed by a
#     restart still happens once the process is back;
#   * a one-shot catch-up shortly after every startup;
#   * a same-day readiness pass at 13:00 ET, well before the 15:30 window,
#     that (re)queues any event in the next few days that is not V4-ready.
RESEARCH_READINESS_CATCHUP_JOB_ID = "research_readiness_catchup"
RESEARCH_PREPARATION_STARTUP_CATCHUP_JOB_ID = "research_preparation_startup_catchup"
_READINESS_CATCHUP_HOUR_ET = 13
_READINESS_CATCHUP_MINUTE_ET = 0
_NIGHTLY_MISFIRE_GRACE_SECONDS = 6 * 3600
_CATCHUP_MISFIRE_GRACE_SECONDS = 2 * 3600
_STARTUP_CATCHUP_DELAY_SECONDS = 90

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
        results = enqueue_preparation_candidates(
            db, options_provider, now=now, us_listing=default_us_listing()
        )
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


def run_research_readiness_catchup_job(*, now: datetime | None = None) -> list[EnqueueResult]:
    """Same-day readiness pass (V4-only reset, 2026-09-02): (re)queues every
    upcoming event in the next few days that is not yet V4-ready -- no
    Company row, or no fresh AI thesis -- so the 15:30 ET decision window
    is not met with RESEARCH_NOT_READY for companies nobody prepared.
    Idempotent and cheap; also used as the one-shot startup catch-up."""
    db = SessionLocal()
    run = start_scheduler_run(db, RESEARCH_READINESS_CATCHUP_JOB_ID)
    try:
        settings = get_settings()
        options_provider = build_options_provider_chain(settings, db=db)
        results = enqueue_readiness_catchup(
            db, options_provider, now=now, us_listing=default_us_listing()
        )
        for result in results:
            record_scheduler_run_event(
                db,
                run,
                calendar_event_id=result.calendar_event_id,
                symbol=result.symbol,
                stage="readiness",
                outcome=result.outcome,
                reason=result.reason,
            )
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_SUCCESS,
            items_evaluated=len(results),
            items_succeeded=sum(1 for r in results if r.outcome in ("queued", "already_ready")),
            items_failed=0,
        )
        return results
    except Exception as exc:  # noqa: BLE001 -- the job must record its own failure
        log.error("research readiness catch-up failed", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        finish_scheduler_run(db, run, status=RUN_STATUS_ERROR, error_summary=_error_summary(exc))
        return []
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
        misfire_grace_time=_NIGHTLY_MISFIRE_GRACE_SECONDS,
        coalesce=True,
    )
    scheduler.add_job(
        run_earnings_research_preparation_job,
        trigger="cron",
        hour=_RESEARCH_PREPARATION_HOUR_UTC,
        minute=_RESEARCH_PREPARATION_MINUTE_UTC,
        id=EARNINGS_RESEARCH_PREPARATION_JOB_ID,
        replace_existing=True,
        misfire_grace_time=_NIGHTLY_MISFIRE_GRACE_SECONDS,
        coalesce=True,
    )
    scheduler.add_job(
        run_research_readiness_catchup_job,
        trigger="cron",
        hour=_READINESS_CATCHUP_HOUR_ET,
        minute=_READINESS_CATCHUP_MINUTE_ET,
        timezone="America/New_York",
        id=RESEARCH_READINESS_CATCHUP_JOB_ID,
        replace_existing=True,
        misfire_grace_time=_CATCHUP_MISFIRE_GRACE_SECONDS,
        coalesce=True,
    )
    scheduler.add_job(
        run_research_readiness_catchup_job,
        trigger="date",
        run_date=datetime.now(UTC) + timedelta(seconds=_STARTUP_CATCHUP_DELAY_SECONDS),
        id=RESEARCH_PREPARATION_STARTUP_CATCHUP_JOB_ID,
        replace_existing=True,
        misfire_grace_time=_CATCHUP_MISFIRE_GRACE_SECONDS,
    )
    scheduler.add_job(
        run_ibkr_gateway_healthcheck_job,
        trigger="interval",
        minutes=IBKR_HEALTHCHECK_INTERVAL_MINUTES,
        id=IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
        replace_existing=True,
    )
    try:
        if get_settings().v4_shadow_enabled:
            # ONE 15:30 ET job (settlement-priority hardening, v4.0.0): it
            # settles every due position first, then begins new decision
            # observations -- see services/v4_shadow_scheduler.py. The two
            # historical ids stay in use as its recorded PHASES only.
            scheduler.add_job(
                run_v4_forward_window_job,
                trigger="cron",
                hour=_V4_DECISION_HOUR_ET,
                minute=_V4_DECISION_MINUTE_ET,
                timezone="America/New_York",
                id=V4_FORWARD_WINDOW_JOB_ID,
                replace_existing=True,
                misfire_grace_time=int(LATE_CUTOFF_GRACE.total_seconds()),
                coalesce=True,
                max_instances=1,
            )
    except Exception:  # noqa: BLE001 -- a V4 registration failure must never take the platform jobs down
        log.error(
            "V4 forward-window job registration failed; platform jobs remain registered",
            exc_info=True,
        )
    return scheduler


#: Job ids that must never fire again. They are deleted from the persistent
#: store by migration (V3: e3a5c7d9b1f2; the split V4 pair: b7d9f1a3c5e7) and,
#: defensively, removed here once the scheduler has loaded its store.
RETIRED_JOB_IDS: tuple[str, ...] = (
    "decision_and_entry_capture",
    "exit_capture",
    *RETIRED_V4_JOB_IDS,
)


def retire_stale_jobs(scheduler: AsyncIOScheduler) -> list[str]:
    """Removes retired job ids that are still present in the loaded job store
    (a stale persistent row would otherwise fire alongside its replacement).
    Returns the ids actually removed."""
    removed: list[str] = []
    for job_id in RETIRED_JOB_IDS:
        try:
            if scheduler.get_job(job_id) is not None:
                scheduler.remove_job(job_id)
                removed.append(job_id)
        except Exception:  # noqa: BLE001 -- best effort; the migration is the guarantee
            log.warning("could not remove retired job %s", job_id, exc_info=True)
    if removed:
        log.warning("removed retired scheduler jobs from the store: %s", ", ".join(removed))
    return removed
