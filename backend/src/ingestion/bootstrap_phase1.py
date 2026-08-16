"""One-shot Phase 1 bootstrap: seed companies and real historical earnings
results for the four V1 tickers directly from SEC EDGAR (free, no key).

Run: uv run python -m ingestion.bootstrap_phase1

Deliberate scope limits (see docs/limitations.md):
  - Only Q1/Q2/Q3 quarterly EPS/revenue are ingested from XBRL. Most filers
    do not separately tag discrete Q4 figures (Q4 has no standalone 10-Q);
    deriving Q4 = FY - Q1 - Q2 - Q3 is analytics, not raw ingestion, and is
    left for Phase 2/4 rather than faked here.
  - `earnings_date` on EarningsEvent is left unset: XBRL only gives us the
    filing date, which is not the same as the actual earnings-release date.
    We do not encode an approximation as if it were a fact.
  - Filing metadata (10-K/10-Q) is captured for all four tickers; full text
    is fetched for one filing only, to prove the FilingsProvider round-trip
    works end to end. Bulk parse/chunk/embed is Phase 5's job.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from core.config import get_settings
from db.session import SessionLocal
from models.company import Company
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.enums import FilingType
from models.filing import Filing
from providers.sec_edgar import SECEdgarProvider
from providers.types import CompanyFactValue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bootstrap_phase1")

TICKERS = ["NVDA", "AMD", "MU", "SNDK"]
_FP_TO_QUARTER = {"Q1": 1, "Q2": 2, "Q3": 3}


def _upsert_company(db: Session, ticker: str, cik: str, name: str) -> Company:
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        company = Company(ticker=ticker, name=name, cik=cik, sector="Semiconductors")
        db.add(company)
        db.flush()
        log.info("created company %s (cik=%s)", ticker, cik)
    return company


def _index_by_period(values: list[CompanyFactValue]) -> dict[tuple[int, str], CompanyFactValue]:
    indexed: dict[tuple[int, str], CompanyFactValue] = {}
    for v in values:
        key = (v.fiscal_year, v.fiscal_period)
        # Prefer the most-recently-filed value for a given period (later
        # amendments supersede earlier ones).
        existing = indexed.get(key)
        if existing is None or v.filed_date > existing.filed_date:
            indexed[key] = v
    return indexed


def _upsert_earnings_event_and_result(
    db: Session,
    company: Company,
    fiscal_year: int,
    fiscal_quarter: int,
    eps: CompanyFactValue,
    revenue: CompanyFactValue | None,
) -> None:
    event = (
        db.query(EarningsEvent)
        .filter_by(company_id=company.id, fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter)
        .one_or_none()
    )
    if event is None:
        event = EarningsEvent(
            company_id=company.id, fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter
        )
        db.add(event)
        db.flush()

    result = db.query(EarningsResult).filter_by(earnings_event_id=event.id).one_or_none()
    retrieved_at = datetime.now(UTC)
    if result is None:
        result = EarningsResult(earnings_event_id=event.id)
        db.add(result)

    result.actual_eps = eps.value
    result.actual_revenue = revenue.value if revenue else None
    result.reported_at = datetime.combine(eps.filed_date, datetime.min.time(), tzinfo=UTC)
    result.source_provider = "sec_edgar_xbrl"
    result.retrieved_at = retrieved_at
    db.flush()
    log.info(
        "  FY%dQ%d: EPS=%s revenue=%s (accn=%s)",
        fiscal_year,
        fiscal_quarter,
        eps.value,
        revenue.value if revenue else "n/a",
        eps.accession_number,
    )


def _upsert_filing(db: Session, company: Company, meta, raw_text: str | None = None) -> None:
    filing = db.query(Filing).filter_by(accession_number=meta.accession_number).one_or_none()
    if filing is None:
        filing = Filing(
            company_id=company.id,
            accession_number=meta.accession_number,
        )
        db.add(filing)
    filing.filing_type = FilingType.FORM_10K if meta.filing_type == "10-K" else FilingType.FORM_10Q
    filing.filing_date = meta.filing_date
    filing.fiscal_period = meta.fiscal_period
    filing.cik = meta.cik
    filing.source_url = meta.source_url
    filing.title = f"{company.ticker} {meta.filing_type} filed {meta.filing_date}"
    filing.retrieved_at = meta.retrieved_at
    if raw_text is not None:
        filing.raw_text = raw_text[:500_000]
    db.flush()
    log.info("  filing %s %s (%s)", meta.filing_type, meta.accession_number, meta.filing_date)


def main() -> None:
    settings = get_settings()
    provider = SECEdgarProvider(user_agent=settings.sec_edgar_user_agent)
    db = SessionLocal()
    fetched_one_filing_text = False

    try:
        for ticker in TICKERS:
            log.info("=== %s ===", ticker)
            cik = provider.lookup_cik(ticker)
            if cik is None:
                log.warning("no CIK found for %s, skipping", ticker)
                continue

            facts = provider.get_company_facts(cik)
            company = _upsert_company(db, ticker, cik, facts.entity_name or ticker)

            eps_by_period = _index_by_period(facts.eps_diluted)
            revenue_by_period = _index_by_period(facts.revenues)

            for (fiscal_year, fp), eps_value in sorted(eps_by_period.items()):
                fiscal_quarter = _FP_TO_QUARTER.get(fp)
                if fiscal_quarter is None:
                    continue  # skip "FY" — see module docstring
                revenue_value = revenue_by_period.get((fiscal_year, fp))
                _upsert_earnings_event_and_result(
                    db, company, fiscal_year, fiscal_quarter, eps_value, revenue_value
                )
            db.commit()

            filings = provider.search_filings(cik, filing_types=["10-K", "10-Q"], limit=4)
            for meta in filings:
                raw_text = None
                if not fetched_one_filing_text:
                    log.info("  fetching full text for %s", meta.accession_number)
                    raw_text = provider.get_filing_text(meta)
                    fetched_one_filing_text = True
                _upsert_filing(db, company, meta, raw_text=raw_text)
            db.commit()

        log.info("bootstrap complete")
    finally:
        db.close()


if __name__ == "__main__":
    main()
