"""Phase 4.9 -- developer-only endpoints to run a real scheduled job on
demand, so the real earnings pipeline (EarningsAPI.com/Finnhub -> earnings
calendar -> decision generation -> entry capture -> settlement) can be
validated without waiting for its daily cron trigger (services/
scheduler.py, all firing at 00:00 UTC / 15:55 America/New_York).

Each endpoint here calls the EXACT SAME job function the scheduler
itself calls -- there is exactly one implementation of "what does the
calendar sync / decision generation / settlement job do", never a
second, parallel "admin version" of that logic. This also means every
existing safety property of those jobs applies unchanged here: real
providers only (no fake data is ever seeded, inserted, or fabricated by
this router -- it only triggers real EarningsAPI.com/Finnhub/IBKR calls
through the already-existing, already-tested pipeline), full idempotency (running
an endpoint twice in a row is always safe -- see services/decision_
pipeline.py, services/benchmark_entry_capture.py, services/benchmark_
exit_capture.py's own idempotency guarantees), and the same honest
per-event failure isolation (one event failing never aborts the batch).

Disabled by default in any production deployment, two layers deep:
api/main.py only registers this router at all when app_env != production
(so a production deployment's /docs doesn't even list these routes),
and every endpoint here also checks settings.app_env itself
(_ensure_enabled below) as defense in depth -- a 404, not a 403, either
way, so nothing about their existence is revealed.
"""

import logging
from datetime import date

from fastapi import APIRouter, Query, status
from sqlalchemy import func

from api.deps import DbSession
from api.exceptions import NotFoundError
from core.config import Settings, get_settings
from models.earnings_calendar_event import EarningsCalendarEvent
from schemas.api import (
    AdminRunEarningsSyncResponse,
    AdminRunResearchPreparationResponse,
)
from services.scheduler import (
    run_earnings_calendar_sync_job,
    run_earnings_research_preparation_job,
)

log = logging.getLogger("api.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


def _ensure_enabled(settings: Settings) -> None:
    if settings.app_env.lower() == "production":
        raise NotFoundError("not found")


def _count(db: DbSession, id_column) -> int:  # noqa: ANN001 -- any InstrumentedAttribute
    return db.query(func.count(id_column)).scalar() or 0


@router.post("/run-earnings-sync", response_model=AdminRunEarningsSyncResponse)
def run_earnings_sync(
    db: DbSession,
    from_date: date | None = Query(  # noqa: B008 -- fastapi.Query is the documented pattern for a query-param default, not a mutable-default bug
        default=None,
        description="Widen the sync backward to this real calendar date (e.g. the start of "
        "the year), instead of the default today-forward window. Never fabricates events for "
        "dates Finnhub doesn't actually have -- see services/earnings_calendar_sync.py.",
    ),
) -> AdminRunEarningsSyncResponse:
    settings = get_settings()
    _ensure_enabled(settings)

    before = _count(db, EarningsCalendarEvent.id)
    log.info("admin: triggering real earnings calendar sync (from_date=%s)", from_date)
    run_earnings_calendar_sync_job(from_date=from_date)
    after = _count(db, EarningsCalendarEvent.id)

    return AdminRunEarningsSyncResponse(
        earnings_calendar_events_before=before, earnings_calendar_events_after=after
    )


@router.post(
    "/run-research-preparation",
    response_model=AdminRunResearchPreparationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_research_preparation(db: DbSession) -> AdminRunResearchPreparationResponse:
    """Pre-live hardening (2026-08-25) -- enqueues durable research-
    preparation jobs for upcoming eligible calendar events (services/
    earnings_research_preparation.py), the same enqueue-only job the
    scheduler runs daily at 01:00 UTC. Returns as soon as enqueueing
    finishes (a handful of cheap DB reads plus one lightweight options-
    chain check per candidate) -- it does NOT wait for, or own the
    lifetime of, the actual preparation work; the dedicated research-
    worker process claims and runs each queued row independently, so an
    HTTP client disconnecting here has zero effect on it. Never
    generates a decision or captures an entry price -- see that
    module's own docstring for the exact boundary. Run this on demand
    to queue today's real candidates immediately rather than waiting
    for tomorrow's scheduled run."""
    settings = get_settings()
    _ensure_enabled(settings)

    log.info("admin: enqueueing real earnings research preparation candidates")
    results = run_earnings_research_preparation_job()

    queued = sum(1 for r in results if r.outcome == "queued")
    already_ready = sum(1 for r in results if r.outcome == "already_ready")
    filtered_out = sum(1 for r in results if r.outcome == "filtered_out")
    preparation_warning = sum(1 for r in results if r.outcome == "preparation_warning")

    return AdminRunResearchPreparationResponse(
        queued=queued,
        already_ready=already_ready,
        filtered_out=filtered_out,
        preparation_warning=preparation_warning,
    )




