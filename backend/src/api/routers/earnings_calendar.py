"""Phase 4.2 -- read-only endpoints over the forward-looking
earnings_calendar_event table. Mounted at ``/earnings-calendar``, not
``/earnings`` -- ``GET /earnings/{symbol}`` would collide with the
existing ``GET /earnings/{event_id}`` route (api/routers/earnings.py),
which is int-keyed and answers a structurally different question
(one already-reported SEC-XBRL event, by id) from this one (a company's
forward-looking Finnhub calendar entries, by symbol). See
PHASE4_ARCHITECTURE_REVIEW.md sec 6 for the router-naming convention
this follows.

No write endpoint here -- Phase 4.2 only adds read access; the sync job
itself is scheduler-only (services/scheduler.py), not exposed over HTTP
yet.
"""

from fastapi import APIRouter, Query

from api.deps import DbSession
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus
from schemas.api import EarningsCalendarEventResponse

router = APIRouter(prefix="/earnings-calendar", tags=["earnings-calendar"])


@router.get("", response_model=list[EarningsCalendarEventResponse])
def list_upcoming_earnings(
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[EarningsCalendarEvent]:
    return (
        db.query(EarningsCalendarEvent)
        .filter(EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING)
        .order_by(EarningsCalendarEvent.earnings_date.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/{symbol}", response_model=list[EarningsCalendarEventResponse])
def get_symbol_earnings_calendar(symbol: str, db: DbSession) -> list[EarningsCalendarEvent]:
    """Every calendar entry on record for ``symbol`` (upcoming and past
    alike) -- an empty list for an unknown or never-synced symbol, not a
    404, matching this project's other list-style endpoints
    (e.g. GET /earnings?ticker=)."""
    return (
        db.query(EarningsCalendarEvent)
        .filter(EarningsCalendarEvent.symbol == symbol.upper())
        .order_by(EarningsCalendarEvent.earnings_date.asc())
        .all()
    )
