"""Provider-boundary data types.

Every external data source returns one of these. Pydantic validates at the
boundary (see project convention: validate external input, trust internal
code) so malformed provider responses fail loudly here instead of silently
propagating into the database.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ProvenancedModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_provider: str
    retrieved_at: datetime


class OHLCBar(ProvenancedModel):
    ticker: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class FilingMetadata(ProvenancedModel):
    cik: str
    company_name: str
    filing_type: str
    filing_date: date
    accession_number: str
    primary_document: str
    source_url: str
    fiscal_period: str | None = None
    # Comma-separated 8-K item codes (e.g. "2.02,9.01"), where SEC provides
    # them. Item 2.02 = "Results of Operations and Financial Condition" —
    # the reliable signal that an 8-K is an earnings-release filing.
    items: str | None = None


class CompanyFactValue(BaseModel):
    fiscal_year: int
    fiscal_period: str  # Q1, Q2, Q3, Q4, FY
    value: Decimal
    unit: str
    filed_date: date
    end_date: date
    accession_number: str
    form: str


class CompanyFacts(ProvenancedModel):
    cik: str
    entity_name: str
    eps_diluted: list[CompanyFactValue]
    revenues: list[CompanyFactValue]


class ConsensusEstimate(ProvenancedModel):
    ticker: str
    fiscal_year: int
    fiscal_quarter: int
    as_of: datetime
    consensus_eps: Decimal | None = None
    consensus_revenue: Decimal | None = None
    analyst_revision_direction: str = "unknown"


class EarningsCalendarEntry(ProvenancedModel):
    ticker: str
    fiscal_year: int
    fiscal_quarter: int
    earnings_date: date
    announcement_time: str = "unknown"
    date_confirmed: bool = False


class OptionQuote(ProvenancedModel):
    ticker: str
    snapshot_timestamp: datetime
    expiration_date: date
    strike: Decimal
    option_type: str  # "call" | "put"
    bid: Decimal | None = None
    ask: Decimal | None = None
    last_price: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None


class TranscriptDocument(ProvenancedModel):
    ticker: str
    fiscal_year: int
    fiscal_quarter: int
    call_date: date
    speakers: list[str]
    text: str
