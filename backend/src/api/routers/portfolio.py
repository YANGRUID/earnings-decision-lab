"""Read-only real IBKR portfolio positions. Serves the most recently
collected snapshot from the database -- never queries the Gateway live on
a request (collection is a separate step, see
ingestion/collect_portfolio_snapshot.py), matching every other real-data
endpoint in this project. READ ONLY: no endpoint here or anywhere in this
codebase places, modifies, or cancels an order.
"""

from fastapi import APIRouter

from api.deps import DbSession
from schemas.api import PortfolioSnapshotResponse
from services.portfolio import get_latest_portfolio_snapshot

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/positions", response_model=PortfolioSnapshotResponse)
def get_portfolio_positions(db: DbSession) -> PortfolioSnapshotResponse:
    positions = get_latest_portfolio_snapshot(db)
    snapshot_timestamp = positions[0].snapshot_timestamp if positions else None
    return PortfolioSnapshotResponse.model_validate(
        {"positions": positions, "snapshot_timestamp": snapshot_timestamp}
    )
