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
from datetime import UTC, datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from analytics.earnings_timing import compute_entry_exit_schedule
from analytics.market_session import EASTERN
from core.config import get_settings
from db.session import SessionLocal, engine
from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.enums import CaptureStatus, EarningsCalendarEventStatus
from models.settlement_capture_attempt import SettlementCaptureAttempt
from providers.factory import build_earnings_calendar_provider, build_options_provider_chain
from rag.embeddings import EmbeddingProvider
from services.benchmark_entry_capture import capture_benchmark_entry
from services.benchmark_exit_capture import (
    _map_timing,  # noqa: PLC2701 -- shared private helper, same BMO/AMC/DMH mapping as the exit service itself
    capture_benchmark_exit,
)
from services.decision_pipeline import run_decision_pipeline_for_event
from services.earnings_calendar_sync import sync_earnings_calendar
from services.llm.factory import get_llm_provider
from services.system_status import get_ibkr_status

log = logging.getLogger("services.scheduler")

CALENDAR_SYNC_JOB_ID = "earnings_calendar_sync"
DECISION_AND_ENTRY_CAPTURE_JOB_ID = "decision_and_entry_capture"
EXIT_CAPTURE_JOB_ID = "exit_capture"
IBKR_GATEWAY_HEALTHCHECK_JOB_ID = "ibkr_gateway_healthcheck"

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
_ENTRY_HOUR_ET = 15
_ENTRY_MINUTE_ET = 55

# NOT passed as a job argument on purpose: SQLAlchemyJobStore pickles a
# job's full spec (id, trigger, func, args) to persist it in
# apscheduler_jobs, and FastEmbedProvider wraps an onnxruntime
# InferenceSession, which is not picklable -- passing it via ``args``
# broke scheduler.start() entirely (confirmed: the whole scheduler,
# including the unrelated calendar-sync job, silently failed to start).
# A plain module-level reference, set once by build_scheduler() and read
# fresh by the job function at call time, never gets pickled at all.
_shared_embedder: EmbeddingProvider | None = None


def run_earnings_calendar_sync_job() -> None:
    """The actual job body -- registered under a fixed id
    (CALENDAR_SYNC_JOB_ID, ``replace_existing=True``) so a restart that
    re-registers it never ends up with two competing schedules. A sync
    provider is unconfigured is logged and skipped, not a crash -- Finnhub
    being unconfigured shouldn't take the scheduler itself down."""
    db = SessionLocal()
    try:
        settings = get_settings()
        provider = build_earnings_calendar_provider(settings, db)
        if provider is None:
            log.warning(
                "earnings calendar sync skipped: no Finnhub provider configured "
                "(set FINNHUB_API_KEY)"
            )
            return
        sync_earnings_calendar(db, provider)
        db.commit()
    except Exception:
        db.rollback()
        log.error("earnings calendar sync job failed", exc_info=True)
    finally:
        db.close()


def run_decision_and_entry_capture_job() -> None:
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
    """
    embedder = _shared_embedder
    if embedder is None:
        log.warning("decision/entry capture run skipped: no embedding model available")
        return

    db = SessionLocal()
    try:
        settings = get_settings()
        llm = get_llm_provider(settings, db=db)
        options_provider = build_options_provider_chain(settings, db=db)

        portfolio = (
            db.query(BenchmarkPortfolio).filter_by(is_active=True).order_by(BenchmarkPortfolio.id).first()
        )
        if portfolio is None:
            log.warning("decision/entry capture run skipped: no active benchmark_portfolio")
            return

        events = (
            db.query(EarningsCalendarEvent)
            .filter(EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING)
            .all()
        )
        now = datetime.now(UTC)
        for event in events:
            try:
                outcome = run_decision_pipeline_for_event(
                    db, event, portfolio, options_provider, llm, embedder, now=now
                )
                db.commit()
            except Exception:
                db.rollback()
                log.error(
                    "decision pipeline failed for calendar_event_id=%s", event.id, exc_info=True
                )
                continue

            if outcome.decision_snapshot_id is None:
                continue
            try:
                decision_snapshot = db.get(DecisionSnapshot, outcome.decision_snapshot_id)
                if decision_snapshot is not None and options_provider is not None:
                    capture_benchmark_entry(
                        db,
                        decision_snapshot=decision_snapshot,
                        portfolio=portfolio,
                        options_provider=options_provider,
                        now=now,
                    )
                    db.commit()
            except Exception:
                db.rollback()
                log.error(
                    "entry capture failed for decision_snapshot_id=%s",
                    outcome.decision_snapshot_id,
                    exc_info=True,
                )
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
    try:
        settings = get_settings()
        options_provider = build_options_provider_chain(settings, db=db)
        if options_provider is None:
            log.warning("exit capture run skipped: no options provider configured")
            return

        portfolio = (
            db.query(BenchmarkPortfolio)
            .filter_by(is_active=True)
            .order_by(BenchmarkPortfolio.id)
            .first()
        )
        if portfolio is None:
            log.warning("exit capture run skipped: no active benchmark_portfolio")
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
            try:
                capture_benchmark_exit(
                    db,
                    decision_snapshot=decision_snapshot,
                    portfolio=portfolio,
                    options_provider=options_provider,
                    now=now,
                )
                db.commit()
            except Exception:
                db.rollback()
                log.error(
                    "exit capture failed for decision_snapshot_id=%s",
                    decision_snapshot.id,
                    exc_info=True,
                )
    finally:
        db.close()


def run_ibkr_gateway_healthcheck_job() -> None:
    """Phase 4.8A -- periodic, low-cost observation of the IBKR Gateway's
    real auth status, independent of the two daily capture jobs. Two real
    purposes (see PHASE4_8A_IBKR_RUNTIME_ARCHITECTURE_REVIEW.md sec
    3.4/5.3): (1) a keep-alive touch well inside IBKR's session-idle
    window, since nothing else calls the Gateway between the two ~15:55
    ET cron fires; (2) a structured, greppable log line every run gives
    an operator real visibility into reconnect/re-auth cycles well
    before the next actual capture window, instead of only discovering a
    dead session from a FAILED capture attempt at 15:55 ET.

    Reuses services.system_status.get_ibkr_status() -- the exact same
    real check the Settings -> Interactive Brokers page already makes --
    rather than a second, parallel auth-status call; the job and the
    status page can never disagree about what "connected" means.

    Deliberately attempts no remediation itself: no call to the IBKR
    Gateway automation container's own activation endpoints or similar.
    Session recovery is that automation layer's own job (IBeam already
    restarts the Gateway process internally on a dropped session); this
    job only observes and reports, matching the capture jobs' own "never
    crash, log and move on" posture. No DB session needed, unlike the
    other three jobs above -- get_ibkr_status() is a pure live HTTP
    check, nothing here reads or writes any table.

    Skips entirely, logged, when OPTIONS_PROVIDER isn't ibkr -- a
    deployment that has never opted into IBKR shouldn't get a recurring
    "gateway unreachable" warning for a Gateway it never configured,
    mirroring run_earnings_calendar_sync_job's own precedent of skipping
    cleanly when its own precondition isn't configured.
    """
    try:
        settings = get_settings()
        if settings.options_provider.lower() != "ibkr":
            log.debug("ibkr gateway healthcheck skipped: OPTIONS_PROVIDER is not ibkr")
            return
        status = get_ibkr_status(settings)
        if status.status_label == "CONNECTED":
            log.info("ibkr gateway healthcheck: %s", status.status_label)
        else:
            log.warning(
                "ibkr gateway healthcheck: %s%s",
                status.status_label,
                f" ({status.error})" if status.error else "",
            )
    except Exception:
        log.error("ibkr gateway healthcheck job failed", exc_info=True)


def build_scheduler(embedder: EmbeddingProvider | None = None) -> AsyncIOScheduler:
    """Constructs (but does not start) the scheduler -- see
    api/main.py::lifespan for the actual start/shutdown lifecycle.
    ``embedder`` is stashed on the module-level ``_shared_embedder``
    (never passed as a job argument -- see run_decision_and_entry_
    capture_job's own docstring for why that broke job persistence)."""
    global _shared_embedder
    _shared_embedder = embedder

    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=engine)},
        timezone="UTC",
    )
    scheduler.add_job(
        run_earnings_calendar_sync_job,
        trigger="cron",
        hour=0,
        minute=0,
        id=CALENDAR_SYNC_JOB_ID,
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
    return scheduler
