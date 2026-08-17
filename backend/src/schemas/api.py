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
    next_term_expiration: date | None
    atm_iv_near: Decimal | None
    atm_iv_next: Decimal | None
    term_structure_slope: Decimal | None
    implied_move_pct: Decimal | None
    implied_move_absolute: Decimal | None
    # Plain arithmetic on stored quotes, never a directional sentiment
    # label -- see analytics/options/sentiment.py.
    put_call_open_interest_ratio: Decimal | None
    put_call_volume_ratio: Decimal | None
    inputs: dict | None
    snapshot_timestamp: datetime
    computed_at: datetime


class HistoricalMoveStatsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sample_size: int
    average_abs_move_pct: Decimal
    median_abs_move_pct: Decimal
    largest_abs_move_pct: Decimal
    largest_move_pct_signed: Decimal


class ImpliedVsRealizedMoveResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    target_earnings_date: date
    snapshot_timestamp: datetime
    near_term_expiration: date | None
    implied_move_pct: Decimal | None
    realized_next_day_move_pct: Decimal


class CompanyReplaySummaryResponse(BaseModel):
    company: CompanyResponse
    historical_moves: HistoricalMoveStatsResponse | None
    # Real implied-vs-realized comparisons accumulated so far -- see
    # services/options_analytics.py.get_implied_vs_realized_moves. Empty
    # for every company today; options_data_ingested on the parent response
    # explains why.
    implied_vs_realized: list[ImpliedVsRealizedMoveResponse]


class ReplaySummaryResponse(BaseModel):
    companies: list[CompanyReplaySummaryResponse]
    # Whether any options-chain quote has ever been ingested for any
    # company -- false today, since no provider on this project's Alpha
    # Vantage plan returns real options data (see
    # providers/alpha_vantage_options.py). The frontend uses this to
    # explain, rather than hide, why implied_vs_realized is empty.
    options_data_ingested: bool


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
    # Populated on every event (unlike market_expectations/implied_move) --
    # this is real history the company already has regardless of whether
    # any options data exists. Never includes this same event's own move.
    # Null only when the company has no *other* reported event with a
    # recorded next_day_move_pct yet. See services/historical_moves.py.
    historical_moves: HistoricalMoveStatsResponse | None = None


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


class DataCountsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    companies: int
    earnings_events: int
    earnings_events_with_results: int
    price_bars: int
    filings: int
    document_chunks: int
    earnings_estimate_snapshots: int
    options_snapshots: int
    volatility_snapshots: int


class DataFreshnessResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    latest_price_bar_date: date | None
    latest_filing_retrieved_at: datetime | None
    latest_earnings_estimate_snapshot_at: datetime | None
    latest_options_snapshot_at: datetime | None


class LlmConfigStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    model: str | None
    configured: bool


class SystemStatusResponse(BaseModel):
    counts: DataCountsResponse
    freshness: DataFreshnessResponse
    llm: LlmConfigStatusResponse
    embedding_model: str
    evaluation: EvaluationStatusResponse


class PortfolioPositionResponse(BaseModel):
    """A real, READ-ONLY brokerage position -- never a market quote (see
    models/portfolio_position_snapshot.py). ``account_id_masked`` is
    already masked before this is ever constructed.
    """

    model_config = ConfigDict(from_attributes=True)

    account_id_masked: str
    conid: int
    contract_description: str
    asset_class: str
    quantity: Decimal
    currency: str | None
    market_price: Decimal | None
    market_value: Decimal | None
    average_cost: Decimal | None
    unrealized_pnl: Decimal | None
    realized_pnl: Decimal | None
    option_expiry: str | None
    option_right: str | None
    option_strike: Decimal | None
    snapshot_timestamp: datetime
    source_provider: str


class PortfolioSnapshotResponse(BaseModel):
    positions: list[PortfolioPositionResponse]
    snapshot_timestamp: datetime | None


class PreparationStepResponse(BaseModel):
    step: str
    status: str
    detail: str | None
    updated_at: datetime


class ResearchJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    company_id: int | None
    status: str
    steps: list[PreparationStepResponse]
    started_at: datetime
    completed_at: datetime | None
    error: str | None


class ResearchJobQueuedResponse(BaseModel):
    """Returned the instant a preparation/refresh run is scheduled, before
    the background task has written its own ResearchPreparationJob row --
    a real, if brief, "queued" state, not a fabricated stand-in for a job
    that doesn't exist yet. Poll GET /research/{symbol}/status for the real
    row once it's created (nearly immediately -- FastAPI runs background
    tasks right after the response is sent).
    """

    ticker: str
    status: str = "queued"


class ResearchOverviewResponse(BaseModel):
    """A cross-section of what's actually on record for a ticker right
    now -- enough for the frontend to decide whether a research workspace
    has anything real to show, without re-deriving freshness/counts logic
    client-side.
    """

    ticker: str
    company: CompanyResponse | None
    latest_job: ResearchJobResponse | None
    earnings_events_count: int
    price_bars_count: int
    filings_count: int
    filing_chunks_count: int
    latest_earnings_estimate: EarningsEstimateResponse | None
    latest_volatility_snapshot: VolatilitySnapshotResponse | None
