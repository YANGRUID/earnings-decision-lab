"""Collects and persists a real, READ-ONLY snapshot of the user's current
IBKR portfolio positions. See services/portfolio.py and
providers/ibkr_portfolio.py. Safe to re-run -- each run adds a new
point-in-time batch rather than overwriting the last one.

Run: uv run python -m ingestion.collect_portfolio_snapshot

Requires the local IBKR Client Portal Gateway to be running and
authenticated (see docs/ibkr_integration.md) -- reports clearly and exits
without crashing if it isn't.
"""

import logging

from core.config import get_settings
from db.session import SessionLocal
from providers.ibkr_client import IBKRError
from providers.ibkr_portfolio import IBKRPortfolioProvider
from services.portfolio import collect_portfolio_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collect_portfolio_snapshot")


def main() -> None:
    settings = get_settings()
    provider = IBKRPortfolioProvider(base_url=settings.ibkr_base_url)
    db = SessionLocal()
    try:
        try:
            rows = collect_portfolio_snapshot(db, provider)
        except IBKRError as exc:
            log.error("could not collect portfolio snapshot: %s", exc)
            return
        if not rows:
            log.info("collected snapshot: account has no open positions")
        else:
            log.info("collected snapshot: %d position(s)", len(rows))
            for row in rows:
                log.info(
                    "  %s (%s) qty=%s mkt_value=%s unrealized_pnl=%s",
                    row.contract_description,
                    row.asset_class,
                    row.quantity,
                    row.market_value,
                    row.unrealized_pnl,
                )
    finally:
        db.close()


if __name__ == "__main__":
    main()
