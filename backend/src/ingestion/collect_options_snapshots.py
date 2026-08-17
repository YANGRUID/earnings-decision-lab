"""Forward options-chain snapshot collection for each covered ticker's next
unreported earnings period, on the T-14/T-7/T-3/T-1 schedule (see
analytics/options/collection_schedule.py). Safe to run daily (e.g. via
cron) -- most days it does nothing, by design: it only fetches on a
scheduled day, and never twice on the same day.

Run: uv run python -m ingestion.collect_options_snapshots

Uses whichever OptionsDataProvider is configured via OPTIONS_PROVIDER (see
providers/factory.py):

- alpha_vantage: **Confirmed live during Phase 12: REALTIME_OPTIONS
  requires a premium subscription this project's key doesn't have** (see
  providers/alpha_vantage_options.py) -- every run logs a
  PremiumEndpointRequiredError for every scheduled ticker and persists
  nothing. Honest, expected, not a bug.
- ibkr: a real Interactive Brokers Client Portal Gateway running locally
  and already authenticated by the user (Phase 13, see
  providers/ibkr_options.py and docs/ibkr_integration.md). If the Gateway
  isn't running or the session isn't authenticated, this is reported
  clearly and the whole run stops early (retrying per-ticker against a
  down/unauthenticated Gateway wastes calls for no benefit, since the
  problem is account-level, not per-ticker) -- it never crashes.
"""

import logging
import time
from datetime import UTC, datetime

from core.config import get_settings
from db.session import SessionLocal
from models.company import Company
from providers.alpha_vantage import AlphaVantageError
from providers.alpha_vantage_options import PremiumEndpointRequiredError
from providers.factory import get_options_provider
from providers.ibkr_client import (
    IBKRCompetingSessionError,
    IBKRError,
    IBKRGatewayUnavailableError,
    IBKRNotAuthenticatedError,
)
from services.market_expectations import get_latest_earnings_estimate
from services.options_analytics import (
    collect_forward_options_snapshot,
    compute_and_persist_volatility_snapshot,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collect_options_snapshots")

TICKERS = ["NVDA", "AMD", "MU", "SNDK"]

# Only applied for Alpha Vantage, whose free tier enforces a real
# ~5/minute, 25/day budget shared across every ingestion script (see
# docs/data_sources.md). IBKR's Gateway is a local loopback process with no
# such cloud-style quota, so this delay would be pure, unnecessary latency
# there.
_ALPHA_VANTAGE_DELAY_BETWEEN_TICKERS_SECONDS = 15.0

# Account-level IBKR failures: retrying against the next ticker can't help
# (the Gateway/session problem applies to every request equally), so the
# whole run stops rather than repeating the same failure four times.
_IBKR_ACCOUNT_LEVEL_ERRORS = (
    IBKRGatewayUnavailableError,
    IBKRNotAuthenticatedError,
    IBKRCompetingSessionError,
)


def main() -> None:
    settings = get_settings()
    try:
        provider = get_options_provider(settings)
    except Exception as exc:
        raise SystemExit(f"could not construct options provider: {exc}") from exc

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

            if i > 0 and settings.options_provider.lower() == "alpha_vantage":
                time.sleep(_ALPHA_VANTAGE_DELAY_BETWEEN_TICKERS_SECONDS)

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
            except _IBKR_ACCOUNT_LEVEL_ERRORS as exc:
                log.error(
                    "stopping run: IBKR Gateway not usable (%s) — see docs/ibkr_integration.md",
                    exc,
                )
                break
            except (AlphaVantageError, IBKRError):
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
