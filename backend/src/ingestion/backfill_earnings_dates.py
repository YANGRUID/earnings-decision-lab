"""Backfill EarningsEvent.period_end_date and earnings_date from SEC EDGAR
alone — no market-data provider involved (see docs/limitations.md for why
that matters right now: the market-data provider is currently blocked).

Run: uv run python -m ingestion.backfill_earnings_dates

- period_end_date comes from the same XBRL facts used in bootstrap_phase1
  (added to the schema after that script ran, so existing events need it
  filled in).
- earnings_date comes from 8-K Item 2.02 filings — see
  ingestion.earnings_date_backfill for the real, sourced (not guessed)
  methodology and its documented proximity-matching heuristic.
"""

import logging
from datetime import date

from sqlalchemy.orm import Session

from core.config import get_settings
from db.session import SessionLocal
from ingestion.earnings_date_backfill import Candidate8K, EventPeriod, match_earnings_dates
from models.company import Company
from models.earnings_event import EarningsEvent
from models.enums import FilingType
from models.filing import Filing
from providers.sec_edgar import SECEdgarProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_earnings_dates")

TICKERS = ["NVDA", "AMD", "MU", "SNDK"]
_FP_TO_QUARTER = {"Q1": 1, "Q2": 2, "Q3": 3}


def _backfill_period_end_dates(db: Session, edgar: SECEdgarProvider, company: Company) -> None:
    missing = (
        db.query(EarningsEvent)
        .filter(EarningsEvent.company_id == company.id, EarningsEvent.period_end_date.is_(None))
        .all()
    )
    if not missing:
        return
    if company.cik is None:
        log.warning("skipping %s: no CIK on record", company.ticker)
        return
    facts = edgar.get_company_facts(company.cik)
    end_date_by_period: dict[tuple[int, int], date] = {}
    for fact in facts.eps_diluted:
        quarter = _FP_TO_QUARTER.get(fact.fiscal_period)
        if quarter is not None:
            end_date_by_period[(fact.fiscal_year, quarter)] = fact.end_date

    for event in missing:
        end_date = end_date_by_period.get((event.fiscal_year, event.fiscal_quarter))
        if end_date is not None:
            event.period_end_date = end_date
    db.commit()


def _backfill_earnings_dates(db: Session, edgar: SECEdgarProvider, company: Company) -> None:
    events = (
        db.query(EarningsEvent)
        .filter(EarningsEvent.company_id == company.id, EarningsEvent.period_end_date.isnot(None))
        .all()
    )
    if not events:
        return
    if company.cik is None:
        log.warning("skipping %s: no CIK on record", company.ticker)
        return

    # 200 is comfortably more than any of these companies file in the ~15-20
    # years of history we have quarterly XBRL data for; SEC's "recent" feed
    # already returns everything in one call, so this costs nothing extra.
    filings_8k = edgar.search_filings(company.cik, filing_types=["8-K"], limit=200)
    candidates = [Candidate8K(f.accession_number, f.filing_date, f.items) for f in filings_8k]
    event_periods = []
    for e in events:
        # Guaranteed by the query filter above (period_end_date.isnot(None)),
        # not re-checked defensively here for its own sake -- asserted so the
        # type checker can see the same guarantee the query already makes.
        assert e.period_end_date is not None
        event_periods.append(
            EventPeriod(
                key=f"{e.fiscal_year}Q{e.fiscal_quarter}", period_end_date=e.period_end_date
            )
        )
    matches = match_earnings_dates(event_periods, candidates)

    filings_by_accn = {f.accession_number: f for f in filings_8k}
    matched_count = 0
    for event in events:
        key = f"{event.fiscal_year}Q{event.fiscal_quarter}"
        match = matches.get(key)
        if match is None or event.date_confirmed:
            continue
        event.earnings_date = match.filing_date
        event.date_confirmed = True
        matched_count += 1

        meta = filings_by_accn[match.accession_number]
        filing = db.query(Filing).filter_by(accession_number=meta.accession_number).one_or_none()
        if filing is None:
            filing = Filing(company_id=company.id, accession_number=meta.accession_number)
            db.add(filing)
        filing.filing_type = FilingType.FORM_8K
        filing.filing_date = meta.filing_date
        filing.cik = meta.cik
        filing.source_url = meta.source_url
        filing.title = f"{company.ticker} 8-K (Item {meta.items}) filed {meta.filing_date}"
        filing.retrieved_at = meta.retrieved_at
    db.commit()
    log.info(
        "  %s: matched %d/%d events to an earnings-release 8-K",
        company.ticker,
        matched_count,
        len(events),
    )


def main() -> None:
    settings = get_settings()
    edgar = SECEdgarProvider(user_agent=settings.sec_edgar_user_agent)
    db = SessionLocal()
    try:
        for ticker in TICKERS:
            log.info("=== %s ===", ticker)
            company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
            if company is None:
                log.warning("  no company row for %s — run bootstrap_phase1 first", ticker)
                continue
            _backfill_period_end_dates(db, edgar, company)
            _backfill_earnings_dates(db, edgar, company)
        log.info("earnings date backfill complete")
    finally:
        db.close()


if __name__ == "__main__":
    main()
