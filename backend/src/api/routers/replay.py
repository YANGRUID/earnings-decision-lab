"""Historical Replay summary: real historical price moves and real
implied-vs-realized comparisons for every covered company, plus an honest
flag explaining why implied_vs_realized is empty today (no options-chain
provider on this project's Alpha Vantage plan returns real data -- see
providers/alpha_vantage_options.py). Never a fabricated options-chain
reconstruction.
"""

from fastapi import APIRouter

from api.deps import DbSession
from models.company import Company
from schemas.api import CompanyReplaySummaryResponse, ReplaySummaryResponse
from services.historical_moves import get_historical_move_stats
from services.options_analytics import get_implied_vs_realized_moves, has_any_options_data

router = APIRouter(prefix="/replay", tags=["replay"])


@router.get("", response_model=ReplaySummaryResponse)
def get_replay_summary(db: DbSession) -> ReplaySummaryResponse:
    companies = db.query(Company).filter(Company.is_active.is_(True)).order_by(Company.ticker).all()

    summaries = [
        CompanyReplaySummaryResponse.model_validate(
            {
                "company": company,
                "historical_moves": get_historical_move_stats(db, company.id),
                "implied_vs_realized": get_implied_vs_realized_moves(db, company.id),
            }
        )
        for company in companies
    ]

    return ReplaySummaryResponse(
        companies=summaries,
        options_data_ingested=has_any_options_data(db),
    )
