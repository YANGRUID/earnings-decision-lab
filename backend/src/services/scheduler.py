"""Phase 4.2 -- the first real background scheduler in this codebase.
``apscheduler>=3.10`` has been a declared dependency since early in this
project but was never imported anywhere until now (every "scheduled"
thing before this was either a cron-invoked standalone script, e.g.
ingestion/collect_options_snapshots.py, or FastAPI's own one-shot
BackgroundTasks) -- see PHASE4_ARCHITECTURE_REVIEW.md sec 4 for the full
evaluation of APScheduler vs. Celery vs. cron that led here. Celery is
deliberately not used: it needs a broker and a separate worker process,
real new infrastructure this personal-scale project doesn't otherwise
need.

``AsyncIOScheduler``, started from api/main.py's existing ``lifespan()``
hook, inside the backend process -- the only component docker-compose
actually guarantees is running continuously. ``SQLAlchemyJobStore``
(against the same Postgres instance, its own auto-created
``apscheduler_jobs`` table) so the job's registration survives a
container restart -- "job survives restart if possible" in the Phase 4.2
brief.

The job function opens its own fresh ``SessionLocal()`` (never reuses a
request-scoped session) and is defensive about the provider it needs
being unconfigured -- the same pattern
ingestion/collect_options_snapshots.py and this project's other
standalone background work already use.
"""

import logging

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.config import get_settings
from db.session import SessionLocal, engine
from providers.factory import build_earnings_calendar_provider
from services.earnings_calendar_sync import sync_earnings_calendar

log = logging.getLogger("services.scheduler")

CALENDAR_SYNC_JOB_ID = "earnings_calendar_sync"


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


def build_scheduler() -> AsyncIOScheduler:
    """Constructs (but does not start) the scheduler -- see
    api/main.py::lifespan for the actual start/shutdown lifecycle."""
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
    return scheduler
