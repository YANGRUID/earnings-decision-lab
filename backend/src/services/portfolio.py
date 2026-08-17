"""Collects and persists real, read-only IBKR portfolio positions. See
providers/ibkr_portfolio.py for the provider that fetches them and
models/portfolio_position_snapshot.py for why this is a separate table
from every market-data snapshot.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from models.portfolio_position_snapshot import PortfolioPositionSnapshot
from providers.ibkr_portfolio import IBKRPortfolioProvider


def collect_portfolio_snapshot(
    db: Session, provider: IBKRPortfolioProvider
) -> list[PortfolioPositionSnapshot]:
    """Fetches every current position and persists one new point-in-time
    batch (all sharing the same snapshot_timestamp) -- never overwrites a
    previous batch, so historical exposure stays real and auditable. An
    account with zero positions persists zero rows and returns an empty
    list, not an error -- a real, valid state, not a failure.
    """
    positions = provider.get_positions()
    snapshot_timestamp = datetime.now(UTC)

    rows = [
        PortfolioPositionSnapshot(
            account_id_masked=position.account_id_masked,
            snapshot_timestamp=snapshot_timestamp,
            conid=position.conid,
            contract_description=position.contract_description,
            asset_class=position.asset_class,
            quantity=position.quantity,
            currency=position.currency,
            market_price=position.market_price,
            market_value=position.market_value,
            average_cost=position.average_cost,
            unrealized_pnl=position.unrealized_pnl,
            realized_pnl=position.realized_pnl,
            option_expiry=position.option_expiry,
            option_right=position.option_right,
            option_strike=position.option_strike,
            source_provider=position.source_provider,
            retrieved_at=position.retrieved_at,
        )
        for position in positions
    ]
    if rows:
        db.add_all(rows)
        db.commit()
    return rows


def get_latest_portfolio_snapshot(db: Session) -> list[PortfolioPositionSnapshot]:
    """Every position row from the most recent snapshot_timestamp on
    record -- "what I currently hold", as of the last time it was
    collected. Empty list if nothing has ever been collected, or if the
    most recent collection genuinely found zero positions.
    """
    latest_timestamp = (
        db.query(PortfolioPositionSnapshot.snapshot_timestamp)
        .order_by(PortfolioPositionSnapshot.snapshot_timestamp.desc())
        .limit(1)
        .scalar()
    )
    if latest_timestamp is None:
        return []
    return (
        db.query(PortfolioPositionSnapshot)
        .filter(PortfolioPositionSnapshot.snapshot_timestamp == latest_timestamp)
        .all()
    )
