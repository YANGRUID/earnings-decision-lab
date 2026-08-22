"""Phase 4.9 -- developer-only endpoints to run a real scheduled job on
demand, so the real earnings pipeline (Finnhub -> earnings calendar ->
decision generation -> entry capture -> settlement) can be validated
without waiting for its daily cron trigger (services/scheduler.py, all
firing at 00:00 UTC / 15:55 America/New_York).

Each endpoint here calls the EXACT SAME job function the scheduler
itself calls -- there is exactly one implementation of "what does the
calendar sync / decision generation / settlement job do", never a
second, parallel "admin version" of that logic. This also means every
existing safety property of those jobs applies unchanged here: real
providers only (no fake data is ever seeded, inserted, or fabricated by
this router -- it only triggers real Finnhub/IBKR calls through the
already-existing, already-tested pipeline), full idempotency (running
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

from fastapi import APIRouter, Query
from sqlalchemy import func

from api.deps import DbSession
from api.exceptions import NotFoundError
from core.config import Settings, get_settings
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.settlement_capture_attempt import SettlementCaptureAttempt
from schemas.api import (
    AdminRunDecisionGenerationResponse,
    AdminRunEarningsSyncResponse,
    AdminRunSettlementCaptureResponse,
)
from services.scheduler import (
    run_decision_and_entry_capture_job,
    run_earnings_calendar_sync_job,
    run_exit_capture_job,
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


@router.post("/run-decision-generation", response_model=AdminRunDecisionGenerationResponse)
def run_decision_generation(db: DbSession) -> AdminRunDecisionGenerationResponse:
    """Decision generation AND entry capture together -- the underlying
    job (Phase 4.4) deliberately does both in one pass so a frozen
    decision's entry price is never captured meaningfully later than its
    own generation; this endpoint preserves that, rather than splitting
    it into two calls that would reintroduce the risk that design
    avoided. Requires real UPCOMING earnings_calendar_event rows to act
    on -- run /admin/run-earnings-sync first if none exist."""
    settings = get_settings()
    _ensure_enabled(settings)

    decisions_before = _count(db, DecisionSnapshot.id)
    entries_before = _count(db, EntryCaptureAttempt.id)
    log.info("admin: triggering real decision generation + entry capture")
    run_decision_and_entry_capture_job()
    decisions_after = _count(db, DecisionSnapshot.id)
    entries_after = _count(db, EntryCaptureAttempt.id)

    return AdminRunDecisionGenerationResponse(
        decision_snapshots_before=decisions_before,
        decision_snapshots_after=decisions_after,
        entry_capture_attempts_before=entries_before,
        entry_capture_attempts_after=entries_after,
    )


@router.post("/run-settlement-capture", response_model=AdminRunSettlementCaptureResponse)
def run_settlement_capture(db: DbSession) -> AdminRunSettlementCaptureResponse:
    """Only acts on decisions with a real CAPTURED entry, due for exit
    today (see run_exit_capture_job's own docstring for the exact real-
    time gating) -- a no-op, honestly, on any day nothing is actually
    due."""
    settings = get_settings()
    _ensure_enabled(settings)

    before = _count(db, SettlementCaptureAttempt.id)
    log.info("admin: triggering real settlement (exit) capture")
    run_exit_capture_job()
    after = _count(db, SettlementCaptureAttempt.id)

    return AdminRunSettlementCaptureResponse(
        settlement_capture_attempts_before=before, settlement_capture_attempts_after=after
    )
