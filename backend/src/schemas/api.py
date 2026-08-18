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
    date_source: str
    """Provenance of estimated_report_date: alpha_vantage (provider-
    confirmed), manual (owner override), estimated, or unknown. Never
    inferred by the client -- always read this before treating a date as
    provider-confirmed."""
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


class ManualEarningsDateRequest(BaseModel):
    """Owner/admin override for a company's next earnings report date --
    see services/market_expectations.py::set_manual_earnings_date."""

    estimated_report_date: date
    fiscal_period_end_date: date | None = None


class VolatilitySnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    method: str
    target_earnings_date: date | None
    anchor: str
    """earnings_anchored or general_current -- see
    models/enums.py::OptionsSnapshotAnchor. When general_current,
    target_earnings_date is None: this snapshot is a current market read,
    not a prediction anchored to a specific earnings date."""
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
    """A single, already-reported (or about-to-be-confirmed) earnings
    event -- strictly retrospective. Deliberately carries no forward-
    looking fields (next-period analyst estimates, implied move): those
    are a property of the *company* right now, not of any specific past
    event, and belong to GET /research/{symbol}/overview instead (see
    api/routers/research.py) -- mixing them into a past event's own page
    is exactly the temporal-context confusion this schema avoids.
    """

    company: CompanyResponse
    result: EarningsResultResponse | None
    price_reaction: PriceReactionResponse | None
    # Populated on every event -- this is real history the company already
    # has regardless of whether any options data exists. Never includes
    # this same event's own move. Null only when the company has no
    # *other* reported event with a recorded next_day_move_pct yet. See
    # services/historical_moves.py.
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
    ticker: str | None = None
    """Optional company context -- when supplied, the persisted history row
    (see AIResearchQuery) is scoped to this ticker; the underlying agent
    itself still answers from whatever the tools retrieve, this only
    affects how the answer is filed for later retrieval."""


class ResearchQueryResponse(BaseModel):
    question: str
    answer: str
    citations: list[CitationResponse]
    trace: ExecutionTraceResponse


class AIResearchHistoryItemResponse(BaseModel):
    """A real, persisted AI Research answer -- see models/ai_research_query.py.
    Selecting one from history restores exactly this stored answer; it is
    never regenerated."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str | None
    question: str
    answer_markdown: str
    citations: list[dict]
    intent_category: str
    planning_method: str
    tool_calls: list[dict]
    verification_ran: bool
    verification_supported: bool | None
    revised: bool
    provider: str
    model: str
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: Decimal | None
    total_duration_ms: float
    created_at: datetime


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


class IbkrStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gateway_reachable: bool
    authenticated: bool
    connected: bool
    competing: bool
    error: str | None


class ProviderCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    prices: bool
    earnings_estimates: bool
    filings: bool
    options: bool
    greeks: bool
    ai: bool


class ProviderStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    domain: str
    configured: bool
    masked_key: str | None
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_status: str | None
    last_error_detail: str | None
    entitlement_note: str | None
    capabilities: ProviderCapabilitiesResponse


class DomainStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    domain: str
    primary: str | None
    fallback: str | None
    primary_is_override: bool
    fallback_is_override: bool
    providers: list[ProviderStatusResponse]


class ProviderDashboardResponse(BaseModel):
    domains: list[DomainStatusResponse]
    strategy_risk_preference: str = "defined_risk_only"


class SystemStatusResponse(BaseModel):
    counts: DataCountsResponse
    freshness: DataFreshnessResponse
    llm: LlmConfigStatusResponse
    embedding_model: str
    evaluation: EvaluationStatusResponse
    ibkr: IbkrStatusResponse
    market_session: str
    providers: ProviderDashboardResponse


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
    retryable: bool | None = None


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


class OptionsMarketStateResponse(BaseModel):
    """The one canonical options-availability answer -- see
    services/options_analytics.py::OptionsMarketState. Every surface that
    shows options status (Dashboard, Company Overview, Upcoming Earnings,
    Strategy Lab) renders this same object rather than each inferring
    availability from a different signal.
    """

    model_config = ConfigDict(from_attributes=True)

    chain_exists: bool
    contract_count: int
    priceable_contract_count: int
    has_bid_ask: bool
    has_iv: bool
    has_greeks: bool
    implied_move_available: bool
    earnings_anchored: bool | None
    expiration: date | None
    source: str | None
    snapshot_timestamp: datetime | None
    snapshot_age_minutes: int | None
    snapshot_age_label: str | None
    market_data_quality: str | None
    data_state: str
    reason: str


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
    latest_price: Decimal | None = None
    historical_moves: HistoricalMoveStatsResponse | None = None
    options_market: OptionsMarketStateResponse


class OptionQuoteResponse(BaseModel):
    expiration_date: date
    strike: Decimal
    option_type: str
    bid: Decimal | None
    ask: Decimal | None
    last_price: Decimal | None
    volume: int | None
    open_interest: int | None
    implied_volatility: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    market_data_quality: str | None
    source_provider: str


class OptionLegResponse(BaseModel):
    option_type: str
    action: str
    strike: Decimal
    premium: Decimal
    quantity: int


class StrategyAnalysisResponse(BaseModel):
    net_premium: Decimal
    max_profit: Decimal | None
    max_loss: Decimal | None
    breakevens: list[Decimal]
    return_on_risk: Decimal | None


class MoveCompatibilityResponse(BaseModel):
    method: str
    sample_size: int
    requires_move_beyond_threshold: bool
    required_move_pct: Decimal
    compatible_count: int
    compatible_pct: Decimal


class ScenarioPnlResponse(BaseModel):
    down_price: Decimal
    down_pnl: Decimal
    flat_pnl: Decimal
    up_price: Decimal
    up_pnl: Decimal


class RankedStrategyResponse(BaseModel):
    rank: int
    category: str
    legs: list[OptionLegResponse]
    analysis: StrategyAnalysisResponse
    score: Decimal
    explanation: str
    scenario: ScenarioPnlResponse | None
    move_compatibility: MoveCompatibilityResponse | None


class StrategyLabResponse(BaseModel):
    """Real, ranked strategy candidates for a company's upcoming earnings,
    built entirely from an already-ingested real options-chain snapshot --
    empty (never fabricated) when no such snapshot exists yet. See
    services/strategy_generation.py and analytics/options/strategy_ranking.py.
    """

    ticker: str
    expiration: date | None
    underlying_price: Decimal | None
    implied_move_pct: Decimal | None
    strategies: list[RankedStrategyResponse]
    chain: list[OptionQuoteResponse]
    anchor: str | None = None
    """earnings_anchored or general_current, mirroring the VolatilitySnapshot
    this was built from -- set whenever real strategies/chain are returned,
    so the frontend never has to guess whether "next earnings date" applies
    to what's shown. None only when there's no chain at all."""
    reason: str | None = None
    """Explanatory context, in two distinct situations that must never be
    confused with each other: (1) ``strategies`` is empty -- *why* (no
    known upcoming earnings date yet, vs. a date is known but no
    options-chain snapshot has been collected for it); (2) ``strategies``
    is real and non-empty but ``anchor="general_current"`` -- a disclaimer
    that what's shown isn't tied to a specific earnings date. ``None`` only
    when real, earnings-anchored strategies are returned with nothing to
    disclaim."""

    market_session: str
    """Real US Eastern-time session right now -- pre_market/regular/
    after_hours/closed (see analytics/market_session.py). Independent of
    whether any chain exists; always set."""
    data_state: str
    """live/delayed/frozen/previous_session/market_closed/not_collected --
    see models/enums.py::DataState and analytics/data_state.py. The single
    honest answer to "how current is what's shown below"."""
    snapshot_source: str | None = None
    """Real source_provider of the chain shown (ibkr/alpha_vantage) --
    None only when there's no chain at all."""
    snapshot_timestamp: datetime | None = None
    snapshot_age_minutes: int | None = None
    snapshot_age_label: str | None = None
    earnings_anchor_status: str
    """confirmed (alpha_vantage-sourced date) / manual (owner override) /
    estimated / unknown -- mirrors EarningsEstimateSnapshot.date_source
    when a date is known, "unknown" when none is. Never presents a
    manual/estimated date as provider-confirmed."""
    options_market: OptionsMarketStateResponse
    """The same canonical options-availability object shown on Company
    Overview / Upcoming Earnings -- so the two surfaces can never disagree
    about whether a real chain exists, how many contracts, or why an
    implied move is or isn't available."""


class EarningsThesisResponse(BaseModel):
    business_context: str
    historical_earnings_pattern: str
    guidance_trend: str
    key_risks: str
    market_setup: str
    disclaimer: str
    citations: list[CitationResponse]
    generated_at: datetime
    model: str


class AIThesisVersionResponse(BaseModel):
    """A real, persisted AI Earnings Thesis generation -- see
    models/ai_thesis_version.py. One row per generation, never overwritten;
    ``is_stale`` (computed by the endpoint, not stored) tells the frontend
    whether newer consensus/options evidence exists than what this version
    was grounded in, without ever silently discarding the old version.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    business_context: str
    historical_earnings_pattern: str
    guidance_trend: str
    key_risks: str
    market_setup: str
    disclaimer: str
    citations: list[dict]
    provider: str
    model: str
    earnings_estimate_snapshot_id: int | None
    volatility_snapshot_id: int | None
    created_at: datetime
    is_stale: bool = False


class DecisionGenerateRequest(BaseModel):
    """Both fields must be given together, or neither -- a manual view
    override (Phase 14.9 Part K) replaces the AI's own direction/
    volatility_view classification but leaves every downstream
    deterministic step (strategy generation, scoring, reasoning)
    unchanged."""

    direction: str | None = None
    volatility_view: str | None = None


class AIDecisionVersionResponse(BaseModel):
    """A real, persisted AI Options Decision -- see
    models/ai_decision_version.py. Append-only: a new generation is
    always a new row (Phase 14.9 Part F), never an edit to a prior one.
    Settlement fields (actual_*, direction_correct, breakeven_met,
    strategy_pnl*) stay null until services/decision_settlement.py has
    real post-earnings data to settle against."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    direction: str
    volatility_view: str
    confidence_score: int
    confidence_components: dict[str, int]
    rationale: str
    bull_case: str
    bear_case: str
    key_catalysts: str
    key_risks: str
    disclaimer: str
    citations: list[dict]
    decision_source: str
    risk_preference: str
    recommended_strategy_category: str | None
    recommended_strategy_legs: list[dict] | None
    recommended_strategy_analysis: dict | None
    recommended_strategy_score: int | None
    recommended_strategy_score_components: dict[str, int] | None
    recommended_strategy_why: list[str] | None
    recommended_strategy_risks: list[str] | None
    alternative_strategies: list[dict] | None
    expiration: date | None
    underlying_price: Decimal | None
    implied_move_pct: Decimal | None
    provider: str
    model: str
    earnings_estimate_snapshot_id: int | None
    volatility_snapshot_id: int | None
    status: str
    is_final: bool
    earnings_event_id: int | None
    actual_next_day_move_pct: Decimal | None
    actual_five_day_move_pct: Decimal | None
    direction_correct: bool | None
    actual_move_exceeded_implied: bool | None
    breakeven_met: bool | None
    strategy_pnl: Decimal | None
    strategy_pnl_available: bool
    settled_at: datetime | None
    created_at: datetime


class RateResponse(BaseModel):
    correct: int
    total: int
    pct: Decimal | None


class ConfidenceBucketResponse(BaseModel):
    label: str
    lower: int
    upper: int
    rate: RateResponse


class TrackRecordResponse(BaseModel):
    """Honest reliability metrics for the AI Options Decision journal --
    see services/track_record.py's module docstring for the precise
    definition of each rate (Directional Accuracy != Breakeven Success !=
    Strategy Win Rate) and why every rate carries its own real sample
    size rather than a bare percentage."""

    window: str
    evaluated_count: int
    directional_accuracy: RateResponse
    bullish_accuracy: RateResponse
    bearish_accuracy: RateResponse
    average_confidence: Decimal | None
    high_confidence_accuracy: RateResponse
    volatility_view_accuracy: RateResponse
    breakeven_success: RateResponse
    strategy_win_rate_available: bool
    confidence_calibration: list[ConfidenceBucketResponse]


class ProviderSettingsUpdateRequest(BaseModel):
    """Every field optional -- only supplied fields are changed. Setting a
    field to null explicitly is not the same as omitting it; use the
    matching ``clear_*`` flag to reset a fallback back to the real env-var
    default. Validated against the real known-provider lists server-side
    (see services/provider_settings.py) -- an unrecognized provider name is
    rejected, never silently accepted.
    """

    price_history_primary: str | None = None
    price_history_fallback: str | None = None
    clear_price_history_fallback: bool = False
    options_primary: str | None = None
    options_fallback: str | None = None
    clear_options_fallback: bool = False
    llm_provider: str | None = None
    llm_model: str | None = None
    strategy_risk_preference: str | None = None


class TestConnectionResponse(BaseModel):
    provider: str
    domain: str
    status: str
    detail: str | None
    tested_at: datetime
