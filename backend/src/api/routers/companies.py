from fastapi import APIRouter, Query

from api.deps import DbSession
from api.exceptions import NotFoundError
from models.company import Company
from schemas.api import CompanyResponse

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanyResponse])
def list_companies(
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[Company]:
    return (
        db.query(Company)
        .filter(Company.is_active.is_(True))
        .order_by(Company.ticker)
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/{ticker}", response_model=CompanyResponse)
def get_company(ticker: str, db: DbSession) -> Company:
    company = db.query(Company).filter(Company.ticker == ticker.upper()).one_or_none()
    if company is None:
        raise NotFoundError(f"company {ticker!r} not found")
    return company
