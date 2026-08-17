from fastapi import APIRouter, Query
from sqlalchemy import tuple_

from api.deps import DbSession
from api.exceptions import NotFoundError
from models.company import Company
from models.earnings_event import EarningsEvent
from schemas.api import EarningsEventDetail, EarningsEventSummary
from services.historical_moves import get_historical_move_stats
from services.market_expectations import get_latest_earnings_estimate
from services.options_analytics import get_latest_volatility_snapshot

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


def _is_most_recent_event(db: DbSession, event: EarningsEvent) -> bool:
    newer_exists = (
        db.query(EarningsEvent.id)
        .filter(
            EarningsEvent.company_id == event.company_id,
            tuple_(EarningsEvent.fiscal_year, EarningsEvent.fiscal_quarter)
            > (event.fiscal_year, event.fiscal_quarter),
        )
        .first()
        is not None
    )
    return not newer_exists


@router.get("/{event_id}", response_model=EarningsEventDetail)
def get_earnings_event(event_id: int, db: DbSession) -> EarningsEventDetail:
    event = db.get(EarningsEvent, event_id)
    if event is None:
        raise NotFoundError(f"earnings event {event_id} not found")

    # Market expectations are about the *next* unreported period, never
    # about the event being viewed (which, if it has a result, already
    # happened) -- so this only ever populates on the company's most
    # recently reported event's page, and only with a real collected
    # snapshot. See services/market_expectations.py.
    market_expectations = None
    implied_move = None
    if _is_most_recent_event(db, event):
        market_expectations = get_latest_earnings_estimate(db, event.company_id)
        implied_move = get_latest_volatility_snapshot(db, event.company_id)

    historical_moves = get_historical_move_stats(db, event.company_id, exclude_event_id=event.id)

    # model_validate on a dict (not the ORM instance directly) so mypy sees
    # a type it can check -- each nested field (company, result, ...) still
    # goes through its own schema's from_attributes=True coercion, since
    # nested Pydantic validation applies each model's own config.
    return EarningsEventDetail.model_validate(
        {
            "id": event.id,
            "fiscal_year": event.fiscal_year,
            "fiscal_quarter": event.fiscal_quarter,
            "earnings_date": event.earnings_date,
            "date_confirmed": event.date_confirmed,
            "company": event.company,
            "result": event.result,
            "price_reaction": event.price_reaction,
            "market_expectations": market_expectations,
            "implied_move": implied_move,
            "historical_moves": historical_moves,
        }
    )
