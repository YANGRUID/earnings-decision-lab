"""Manually captures a real options-chain snapshot for every researched
company, tagged as a deliberate market-close snapshot (Phase 14.10 Part
D4) -- the only code path that ever sets OptionsSnapshotPurpose.CLOSE,
which is what lets the snapshot-selection fallback later say "Previous
close" honestly instead of the more conservative "Previous-session
snapshot".

Run manually, or via a user-owned cron/launchd job timed near a real
market close (e.g. 16:05 America/New_York on a trading day), while the
IBKR Client Portal Gateway happens to be running and authenticated:

    uv run python -m ingestion.capture_close_snapshot

This project makes no always-on guarantee here -- the Gateway requires
local authentication and the machine may be asleep or offline at close.
A missed close is simply a missed close; nothing here invents one after
the fact.
"""

import logging
from datetime import UTC, datetime

from core.config import get_settings
from db.session import SessionLocal
from models.company import Company
from providers.alpha_vantage import AlphaVantageError
from providers.factory import get_options_provider
from providers.ibkr_client import (
    IBKRCompetingSessionError,
    IBKRError,
    IBKRGatewayUnavailableError,
    IBKRNotAuthenticatedError,
)
from services.market_expectations import get_latest_earnings_estimate
from services.options_analytics import capture_close_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("capture_close_snapshot")

# Account-level IBKR failures: retrying against the next company can't
# help (the Gateway/session problem applies equally to every request), so
# the whole run stops rather than repeating the same failure per company.
_IBKR_ACCOUNT_LEVEL_ERRORS = (
    IBKRGatewayUnavailableError,
    IBKRNotAuthenticatedError,
    IBKRCompetingSessionError,
)


def main() -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        try:
            provider = get_options_provider(settings, db=db)
        except Exception as exc:
            raise SystemExit(f"could not construct options provider: {exc}") from exc

        companies = db.query(Company).order_by(Company.ticker).all()
        if not companies:
            log.info("no researched companies on record yet -- nothing to capture")
            return

        as_of = datetime.now(UTC)
        for company in companies:
            estimate = get_latest_earnings_estimate(db, company.id)
            earnings_date = (
                estimate.estimated_report_date
                if estimate is not None and estimate.estimated_report_date is not None
                else None
            )
            try:
                snapshots = capture_close_snapshot(db, provider, company, earnings_date, as_of)
            except _IBKR_ACCOUNT_LEVEL_ERRORS as exc:
                log.error(
                    "stopping run: IBKR Gateway not usable (%s) -- see docs/ibkr_integration.md",
                    exc,
                )
                break
            except (AlphaVantageError, IBKRError):
                log.exception("%s: close snapshot capture failed", company.ticker)
                continue

            if snapshots is None:
                log.info("%s: already captured a snapshot today, skipping", company.ticker)
                continue
            log.info(
                "%s: captured %d option quotes as a close snapshot",
                company.ticker,
                len(snapshots),
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
