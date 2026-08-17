from fastapi import APIRouter, Query

from api.deps import DbSession
from api.exceptions import NotFoundError
from models.company import Company
from models.earnings_event import EarningsEvent
from schemas.api import EarningsEventDetail, EarningsEventSummary

router = APIRouter(prefix="/earnings", tags=["earnings"])


@router.get("", response_model=list[EarningsEventSummary])
def list_earnings(
    db: DbSession,
    ticker: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[EarningsEvent]:
    query = db.query(EarningsEvent)
    if ticker:
        query = query.join(Company).filter(Company.ticker == ticker.upper())
    query = query.order_by(EarningsEvent.fiscal_year.desc(), EarningsEvent.fiscal_quarter.desc())
    return query.offset(offset).limit(limit).all()


@router.get("/{event_id}", response_model=EarningsEventDetail)
def get_earnings_event(event_id: int, db: DbSession) -> EarningsEvent:
    event = db.get(EarningsEvent, event_id)
    if event is None:
        raise NotFoundError(f"earnings event {event_id} not found")
    return event
