"""Forward options-chain snapshot collection for each covered ticker's next
unreported earnings period, on the T-14/T-7/T-3/T-1 schedule (see
analytics/options/collection_schedule.py). Safe to run daily (e.g. via
cron) -- most days it does nothing, by design: it only fetches on a
scheduled day, and never twice on the same day.

Run: uv run python -m ingestion.collect_options_snapshots

**Confirmed live during Phase 12: Alpha Vantage's REALTIME_OPTIONS requires
a premium subscription this project's key doesn't have** (see
providers/alpha_vantage_options.py) -- so today, every run logs a
PremiumEndpointRequiredError for every scheduled ticker and persists
nothing. That's the honest, expected outcome, not a bug in this script;
this is ready to collect and derive real implied-move/ATM-IV/sentiment
data the moment a subscription exists, with zero code changes.

One real Alpha Vantage API call per scheduled ticker (REALTIME_OPTIONS),
against the same shared free-tier budget as the other Alpha Vantage
ingestion scripts (~5/minute, 25/day) -- hence the delay between tickers.
"""

import logging
import time
from datetime import UTC, datetime

from core.config import get_settings
from db.session import SessionLocal
from models.company import Company
from providers.alpha_vantage import AlphaVantageError
from providers.alpha_vantage_options import (
    AlphaVantageOptionsProvider,
    PremiumEndpointRequiredError,
)
from services.market_expectations import get_latest_earnings_estimate
from services.options_analytics import (
    collect_forward_options_snapshot,
    compute_and_persist_volatility_snapshot,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collect_options_snapshots")

TICKERS = ["NVDA", "AMD", "MU", "SNDK"]
_DELAY_BETWEEN_TICKERS_SECONDS = 15.0


def main() -> None:
    settings = get_settings()
    if not settings.alpha_vantage_api_key:
        raise SystemExit("ALPHA_VANTAGE_API_KEY is not configured — see .env.example")
    provider = AlphaVantageOptionsProvider(api_key=settings.alpha_vantage_api_key)
    db = SessionLocal()
    try:
        for i, ticker in enumerate(TICKERS):
            company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
            if company is None:
                log.warning("skipping %s: no covered Company row", ticker)
                continue

            estimate = get_latest_earnings_estimate(db, company.id)
            if estimate is None or estimate.estimated_report_date is None:
                log.info("%s: no known upcoming earnings date, nothing to schedule against", ticker)
                continue

            if i > 0:
                time.sleep(_DELAY_BETWEEN_TICKERS_SECONDS)

            as_of = datetime.now(UTC)
            try:
                snapshots = collect_forward_options_snapshot(
                    db, provider, company, estimate.estimated_report_date, as_of
                )
            except PremiumEndpointRequiredError:
                log.info(
                    "%s: REALTIME_OPTIONS requires a premium Alpha Vantage plan — "
                    "expected on this project's current plan, see docs/data_sources.md",
                    ticker,
                )
                continue
            except AlphaVantageError:
                log.exception("%s: options snapshot fetch failed", ticker)
                continue

            if snapshots is None:
                log.info(
                    "%s: not a scheduled collection day for earnings date %s",
                    ticker,
                    estimate.estimated_report_date,
                )
                continue

            log.info("%s: persisted %d option quotes", ticker, len(snapshots))
            volatility_snapshot = compute_and_persist_volatility_snapshot(
                db, company, estimate.estimated_report_date
            )
            if volatility_snapshot is not None:
                log.info(
                    "%s: implied move %.4f%% (ATM IV %s)",
                    ticker,
                    (volatility_snapshot.implied_move_pct or 0) * 100,
                    volatility_snapshot.atm_iv_near,
                )
    finally:
        db.close()


if __name__ == "__main__":
    main()
