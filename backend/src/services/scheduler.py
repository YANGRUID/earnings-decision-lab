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

from core.config import get_settings
from db.session import SessionLocal, engine
from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus
from providers.factory import build_earnings_calendar_provider, build_options_provider_chain
from rag.embeddings import EmbeddingProvider
from services.benchmark_entry_capture import capture_benchmark_entry
from services.decision_pipeline import run_decision_pipeline_for_event
from services.earnings_calendar_sync import sync_earnings_calendar
from services.llm.factory import get_llm_provider

log = logging.getLogger("services.scheduler")

CALENDAR_SYNC_JOB_ID = "earnings_calendar_sync"
DECISION_AND_ENTRY_CAPTURE_JOB_ID = "decision_and_entry_capture"

# The real, fixed wall-clock trigger every eligible event's entry_
# timestamp resolves to (analytics/earnings_timing.py::ENTRY_EXIT_TIME)
# -- only the *date* varies per event, never the time, so one daily cron
# job in America/New_York (not the scheduler's default UTC) is
# sufficient; no continuous polling needed.
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
    return scheduler
