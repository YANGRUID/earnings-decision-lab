"""API response schemas. Deliberately separate from internal ORM models and
from the extraction/agent schemas — the wire contract is allowed to differ
from internal representations without those changing in lockstep.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from evaluation.models import EvaluationRun
from rag.context import Citation


class CompanyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    name: str
    sector: str | None
    exchange: str | None


class EarningsResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    actual_eps: Decimal | None
    actual_revenue: Decimal | None
    gross_margin: Decimal | None
    reported_at: datetime | None
    source_provider: str


class PriceReactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    close_price_before: Decimal | None
    next_day_close: Decimal | None
    five_day_close: Decimal | None
    next_day_move_pct: Decimal | None
    five_day_move_pct: Decimal | None


class EarningsEventSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fiscal_year: int
    fiscal_quarter: int
    earnings_date: date | None
    date_confirmed: bool


class EarningsEstimateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fiscal_period_end_date: date
    horizon: str
    estimated_report_date: date | None
    eps_estimate_average: Decimal | None
    eps_estimate_high: Decimal | None
    eps_estimate_low: Decimal | None
    eps_estimate_analyst_count: int | None
    eps_revision_direction: str
    revenue_estimate_average: Decimal | None
    revenue_estimate_high: Decimal | None
    revenue_estimate_low: Decimal | None
    revenue_estimate_analyst_count: int | None
    revenue_revision_direction: str
    snapshot_timestamp: datetime
    source_provider: str


class VolatilitySnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    method: str
    near_term_expiration: date | None
    atm_iv_near: Decimal | None
    implied_move_pct: Decimal | None
    implied_move_absolute: Decimal | None
    inputs: dict | None
    snapshot_timestamp: datetime
    computed_at: datetime


class EarningsEventDetail(EarningsEventSummary):
    company: CompanyResponse
    result: EarningsResultResponse | None
    price_reaction: PriceReactionResponse | None
    # Only populated when this is the company's most recently reported
    # event and a real estimate snapshot has been collected for the next
    # (unreported) period -- see api/routers/earnings.py and
    # services/market_expectations.py. Never about *this* event's own
    # since-reported quarter, which has no meaningful "expectation" left.
    market_expectations: EarningsEstimateResponse | None = None
    # Same "most recently reported event only" rule as market_expectations,
    # and for the same reason -- see api/routers/earnings.py and
    # services/options_analytics.py. Null whenever no options-chain data has
    # been ingested for this company yet (true for every company today,
    # since Alpha Vantage's options endpoints are premium-gated on this
    # project's plan -- see providers/alpha_vantage_options.py).
    implied_move: VolatilitySnapshotResponse | None = None


class CitationResponse(BaseModel):
    marker: str
    ticker: str
    filing_type: str
    filing_date: date
    section: str | None
    source_url: str

    @classmethod
    def from_citation(cls, citation: Citation) -> "CitationResponse":
        return cls(
            marker=citation.marker,
            ticker=citation.ticker,
            filing_type=citation.filing_type,
            filing_date=citation.filing_date,
            section=citation.section,
            source_url=citation.source_url,
        )


class FilingSearchResponse(BaseModel):
    context_text: str
    citations: list[CitationResponse]


class ToolCallResponse(BaseModel):
    tool_name: str
    arguments: dict
    success: bool
    duration_ms: float
    summary: str
    error: str | None
    query_description: str | None


class ExecutionTraceResponse(BaseModel):
    intent_category: str
    planning_method: str
    tool_calls: list[ToolCallResponse]
    verification_ran: bool
    verification_supported: bool | None
    revised: bool
    model: str
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: Decimal | None
    total_duration_ms: float


class ResearchQueryRequest(BaseModel):
    question: str


class ResearchQueryResponse(BaseModel):
    question: str
    answer: str
    citations: list[CitationResponse]
    trace: ExecutionTraceResponse


class EvaluationStatusResponse(BaseModel):
    """Wraps evaluation.models.EvaluationRun with an explicit ``available``
    flag rather than raising 404 -- no evaluation run exists at all on a
    fresh clone or in CI (evaluation/results/*.json is real output, not
    committed, see docs/evaluation.md), and that's an honest state to
    represent, not an error.
    """

    available: bool
    run: EvaluationRun | None
