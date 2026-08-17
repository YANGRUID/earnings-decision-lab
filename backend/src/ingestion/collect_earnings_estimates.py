"""Collects real analyst-consensus data for each covered ticker's next
unreported earnings period from Alpha Vantage and persists it as a new
EarningsEstimateSnapshot row. Safe to re-run -- each run adds a new
point-in-time snapshot rather than overwriting the last one.

Run: uv run python -m ingestion.collect_earnings_estimates

Two real Alpha Vantage API calls per ticker (EARNINGS_ESTIMATES +
EARNINGS_CALENDAR) -- 8 calls total for the 4 covered tickers, against a
free-tier budget of ~5/minute and 25/day (see docs/data_sources.md), hence
the deliberate delay between tickers below.
"""

import logging
import time

from core.config import get_settings
from db.session import SessionLocal
from models.company import Company
from providers.alpha_vantage_estimates import AlphaVantageEarningsEstimatesProvider
from services.market_expectations import collect_next_earnings_estimate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collect_earnings_estimates")

TICKERS = ["NVDA", "AMD", "MU", "SNDK"]
_DELAY_BETWEEN_TICKERS_SECONDS = 15.0


def main() -> None:
    settings = get_settings()
    if not settings.alpha_vantage_api_key:
        raise SystemExit("ALPHA_VANTAGE_API_KEY is not configured — see .env.example")
    provider = AlphaVantageEarningsEstimatesProvider(api_key=settings.alpha_vantage_api_key)
    db = SessionLocal()
    try:
        for i, ticker in enumerate(TICKERS):
            company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
            if company is None:
                log.warning("skipping %s: no covered Company row", ticker)
                continue

            if i > 0:
                time.sleep(_DELAY_BETWEEN_TICKERS_SECONDS)

            try:
                row = collect_next_earnings_estimate(db, provider, company)
            except Exception:
                log.exception("failed to collect earnings estimate for %s", ticker)
                continue

            if row is None:
                log.info("%s: provider has no upcoming earnings date", ticker)
            else:
                log.info(
                    "%s: snapshot for period ending %s (report date %s), "
                    "eps_avg=%s revenue_avg=%s",
                    ticker,
                    row.fiscal_period_end_date,
                    row.estimated_report_date,
                    row.eps_estimate_average,
                    row.revenue_estimate_average,
                )
    finally:
        db.close()


if __name__ == "__main__":
    main()
