"""API response schemas. Deliberately separate from internal ORM models and
from the extraction/agent schemas — the wire contract is allowed to differ
from internal representations without those changing in lockstep.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

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












class EarningsCalendarEventResponse(BaseModel):
    """Phase 4.2 -- one row from the forward-looking, Finnhub-sourced
    earnings_calendar_event table. Deliberately a distinct schema from
    EarningsEventSummary/EarningsEventDetail below, which describe the
    existing retrospective, SEC-XBRL-sourced earnings_event table -- the
    two are different tables with different provenance and grain, not two
    views of the same data (see models/earnings_calendar_event.py).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    company_name: str
    logo_url: str | None
    earnings_date: date
    earnings_time: str
    eps_estimate: Decimal | None
    revenue_estimate: Decimal | None
    market_cap: Decimal | None
    source: str
    status: str


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
    """Explicit company context -- e.g. the Research page's own URL
    ?ticker= param. Always trusted first by
    services.research_query_resolution.resolve_mentioned_companies over
    whatever the question text itself happens to mention (Part A3/A5,
    2026-08-26); still combined with, not a replacement for, tickers
    the question text names on its own (Part A6 multi-company support)."""
    as_of: date | None = None
    """Point-in-time cutoff (Part A8) -- when set, retrieval never sees a
    filing/earnings event dated after this. None (every real caller
    today) means "as of now", unchanged behavior."""


# Part A11 -- an honest, typed state instead of ever hallucinating an
# answer the system doesn't actually have. "completed" is the only status
# that carries a real answer/citations/trace; every other status means
# exactly what its name says and nothing was fabricated to fill the gap.
ResearchQueryStatus = Literal[
    "completed",
    "preparing",
    "insufficient_evidence",
    "company_not_found",
    "research_failed",
]


class PreparingCompanyResponse(BaseModel):
    """One company whose research isn't ready yet -- already enqueued
    (or already in flight) on the same durable queue the automated
    scheduler uses (services.earnings_research_preparation.
    enqueue_ticker_for_preparation), never prepared synchronously inside
    this request."""

    ticker: str
    job_id: int
    job_status: str


class ResearchQueryResponse(BaseModel):
    question: str
    status: ResearchQueryStatus = "completed"
    answer: str | None = None
    citations: list[CitationResponse] = []
    trace: ExecutionTraceResponse | None = None
    preparing: list[PreparingCompanyResponse] = []
    unresolved_tickers: list[str] = []


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
    # Phase 4.8A -- see services/system_status.py::ibkr_status_label.
    status_label: str


class TwsStatusResponse(BaseModel):
    """IBKR TWS Migration Phase 1, Section 35 -- the additive TWS-transport
    sibling of IbkrStatusResponse above (that one, the existing Web/
    Client-Portal-Gateway status, is UNCHANGED by this migration). Never
    carries account id, username, or session secrets -- see services/
    system_status.py::get_tws_status."""

    model_config = ConfigDict(from_attributes=True)

    configured: bool
    gateway_reachable: bool
    socket_connected: bool
    api_ready: bool
    market_data_quality: str | None
    error: str | None
    status_label: str
    # IBKR TWS Migration, Phase 3 readiness (Section 21/42) -- additive,
    # see services/system_status.py::TwsStatus's own comment.
    last_heartbeat: datetime | None
    reconnect_state: str


class TwsProductionSanityResponse(BaseModel):
    """IBKR TWS Migration, production cutover -- GET /internal/ibkr/tws-
    production-sanity's read-only result. See api/routers/tws_diagnostics.py
    for why this runs in-process. Carries only market data and connection
    state: never an account id, username, or session token."""

    shared_provider_reused: bool
    reconnect_state: str
    api_ready: bool

    underlying_ticker: str
    underlying_price: Decimal
    underlying_bid: Decimal | None
    underlying_ask: Decimal | None
    underlying_quality: str | None
    underlying_source_provider: str | None
    underlying_elapsed_ms: float

    option_expiration: date
    option_strike: Decimal
    option_right: str
    option_conid: int
    option_bid: Decimal | None
    option_ask: Decimal | None
    option_last: Decimal | None
    option_quality: str | None
    option_source_provider: str | None
    option_elapsed_ms: float


class IbkrConnectResponse(BaseModel):
    """Phase 4.8A -- GET /ibkr/connect's only response shape: a browser-
    facing URL, nothing else. See api/routers/ibkr.py."""

    url: str


class ProviderCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    prices: bool
    earnings_estimates: bool
    earnings_calendar: bool
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


class V4DecisionViewModelResponse(BaseModel):
    """The explicit V4 DecisionView model configuration (2026-09-02) as the
    Settings page shows it -- read-only here; it is set in the environment."""

    provider: str | None
    model: str | None
    thinking: str | None
    reasoning_effort: str | None
    max_tokens: int | None
    config_version: str | None
    config_error: str | None


class ProviderDashboardResponse(BaseModel):
    domains: list[DomainStatusResponse]
    strategy_risk_preference: str = "defined_risk_only"
    v4_decision_view: V4DecisionViewModelResponse | None = None


class ProviderUsageSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    domain: str
    request_count: int
    success_count: int
    error_count: int
    rate_limited_count: int
    avg_latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost: Decimal | None
    last_event_at: datetime | None


class UsageSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    window: str
    since: datetime | None
    total_requests: int
    total_errors: int
    total_rate_limited: int
    total_llm_tokens: int | None
    total_estimated_cost: Decimal | None
    providers: list[ProviderUsageSummaryResponse]


class ProviderCredentialUpdateRequest(BaseModel):
    """Add/replace a provider's stored API key. ``base_url``/``model`` only
    apply to openai_compatible -- ignored for every other provider. Never
    echoed back in any response; see services/provider_credentials.py and
    services/secret_store/."""

    api_key: str
    base_url: str | None = None
    model: str | None = None


class SchedulerJobStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    next_run_time: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None


class SchedulerStatusResponse(BaseModel):
    """Phase 4.9 -- real, live introspection of the actual running
    scheduler (services/scheduler.py::get_scheduler_status) -- never
    assumed running just because the process is up."""

    model_config = ConfigDict(from_attributes=True)

    running: bool
    jobs: list[SchedulerJobStatusResponse]


class SystemStatusResponse(BaseModel):
    counts: DataCountsResponse
    freshness: DataFreshnessResponse
    llm: LlmConfigStatusResponse
    embedding_model: str
    evaluation: EvaluationStatusResponse
    ibkr: IbkrStatusResponse
    tws: TwsStatusResponse
    scheduler: SchedulerStatusResponse
    market_session: str
    providers: ProviderDashboardResponse






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
    bid_ask_contract_count: int
    iv_contract_count: int
    greeks_contract_count: int
    volume_coverage: float
    oi_coverage: float
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
    snapshot_tier: str
    is_fallback_snapshot: bool
    snapshot_purpose: str | None
    actionability: str
    underlying_price: Decimal | None
    underlying_timestamp: datetime | None


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



class ResearchOverviewListResponse(BaseModel):
    """Every researched company's overview in ONE response (Company Search).
    Built strictly from persisted state -- no provider call, no recompute --
    so a list page can never fan out per-ticker requests (V4-only reset,
    2026-09-02: that fan-out was the SPA navigation stall)."""

    overviews: list[ResearchOverviewResponse]

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


class EstimatedProbabilityResponse(BaseModel):
    """Options Decision Engine V3 Part E -- the SAME percentage as
    MoveCompatibilityResponse.compatible_pct, wrapped with a Wilson
    confidence interval and a small-sample flag. Never a second,
    independently computed number -- see
    analytics/decision/probability.py."""

    method: str
    sample_size: int
    compatible_count: int
    probability: Decimal
    low_sample_confidence: bool
    wilson_lower: Decimal | None
    wilson_upper: Decimal | None










class ScenarioPnlResponse(BaseModel):
    down_price: Decimal
    down_pnl: Decimal
    flat_pnl: Decimal
    up_price: Decimal
    up_pnl: Decimal


class RankedStrategyResponse(BaseModel):
    """Strategy Lab is market-focused: real candidates, pricing, and
    payoff analysis only -- never budget sizing (Phase 14.12). Trade
    Budget / Max Risk / position sizing live exclusively in AI Decision,
    which is the only place a personal risk tolerance and dollar amount
    belong."""

    rank: int
    category: str
    legs: list[OptionLegResponse]
    analysis: StrategyAnalysisResponse
    score: Decimal
    explanation: str
    scenario: ScenarioPnlResponse | None
    move_compatibility: MoveCompatibilityResponse | None




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
    """``direction``/``volatility_view`` must be given together, or
    neither -- a manual view override (Phase 14.9 Part K) replaces the
    AI's own direction/volatility_view classification but leaves every
    downstream deterministic step (strategy generation, scoring,
    reasoning) unchanged.

    ``trade_budget`` (Phase 14.10 Part G), when given, restricts the
    recommended/alternative candidates to ones actually affordable at
    that budget -- see analytics/decision/budget.py. ``risk_cap`` further
    restricts usable risk capital below the budget itself, either as a
    dollar amount (``risk_cap_is_percent=False``) or a percentage of
    ``trade_budget`` (``risk_cap_is_percent=True``).

    ``risk_profile`` (Options Decision Engine V3 Part D) -- "conservative"
    | "moderate" | "aggressive" -- is selected PER DECISION, not a single
    global setting; omitted defaults from the global StrategyRiskPreference
    app setting. See analytics/decision/risk_profile.py.

    ``expiration`` (Options Decision Engine V3 Part H/I), when given,
    fetches the real chain for exactly that expiration (Manual mode) --
    see services/decision_engine.py::generate_decision's
    ``manual_expiration`` parameter. Omitted means Auto (the existing
    resolver-driven pick, unchanged).
    """

    direction: str | None = None
    volatility_view: str | None = None
    trade_budget: Decimal | None = None
    risk_cap: Decimal | None = None
    risk_cap_is_percent: bool = False
    risk_profile: str | None = None
    expiration: date | None = None










class RateResponse(BaseModel):
    correct: int
    total: int
    pct: Decimal | None


class ConfidenceBucketResponse(BaseModel):
    label: str
    lower: int
    upper: int
    rate: RateResponse




class StandardizedCohortSummaryResponse(BaseModel):
    n: int
    wins: int
    losses: int
    mean_return_on_standardized_capital: Decimal | None
    median_return_on_standardized_capital: Decimal | None
    total_realized_pnl: Decimal
    portfolio_drawdown_available: bool
    portfolio_drawdown_reason: str








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


class AdminRunEarningsSyncResponse(BaseModel):
    """Phase 4.9 -- POST /admin/run-earnings-sync. Real before/after
    counts from a real, immediate run of run_earnings_calendar_sync_job
    (services/scheduler.py) against the real, configured Finnhub
    provider -- never a fabricated or estimated number."""

    earnings_calendar_events_before: int
    earnings_calendar_events_after: int


class V4CompatibilityResponse(BaseModel):
    """V4.2 -- GET /v4/experimental/compatibility. A pure computation
    over analytics/decision/v4_compatibility.py, never a recommendation
    and never touching any real trading table -- see that router's own
    docstring for why it is registered only outside production."""

    direction: str
    volatility_view: str | None
    expected_move_intent: str
    strategy: str
    direction_compatibility: float
    move_magnitude_compatibility: float
    volatility_compatibility: float
    payoff_shape_compatibility: float
    overall_semantic_compatibility: float
    tier: str
    reason_codes: list[str]
    explanation: str


class V4StrikeLegResponse(BaseModel):
    """One leg of a V4.3 strike-selection result -- see
    analytics/decision/v4_strike_engine.py::V4Leg."""

    action: str
    right: str
    quantity: int
    target_price: Decimal
    target_rationale: str
    selected_strike: Decimal | None
    target_distance_dollars: Decimal | None
    target_distance_pct: Decimal | None
    moneyness_pct: Decimal | None
    expected_move_units: Decimal | None
    external_contract_id: str | None
    quote_quality: str | None
    spread_pct: Decimal | None
    volume: int | None
    open_interest: int | None
    reason_codes: list[str]


class V4StrikeSelectionResponse(BaseModel):
    """V4.3 -- GET /v4/experimental/strike-selection. A pure computation
    over analytics/decision/v4_strike_engine.py against a SYNTHETIC
    chain built from the query parameters -- never a real captured
    chain and never a recommendation, see that router's own docstring."""

    strategy: str
    status: str
    spot: Decimal
    implied_move_dollars: Decimal | None
    implied_move_pct: Decimal | None
    historical_median_abs_move_pct: Decimal | None
    legs: list[V4StrikeLegResponse]
    center_target: Decimal | None
    lower_boundary: Decimal | None
    upper_boundary: Decimal | None
    width: Decimal | None
    width_pct_of_spot: Decimal | None
    width_in_expected_move_units: Decimal | None
    symmetry_error_pct: Decimal | None
    reason_codes: list[str]
    explanation: str
    engine_version: str


class V4T1ScenarioPointResponse(BaseModel):
    """One point in the V4.4A scenario grid -- see
    analytics/decision/v4_t1_pricing.py::T1ScenarioResult."""

    scenario_id: str
    underlying_move_label: str
    scenario_underlying_price: Decimal
    iv_scenario_label: str
    theoretical_liquidation_value: Decimal | None
    executable_liquidation_value: Decimal | None
    realized_equivalent_pnl_theoretical: Decimal | None
    realized_equivalent_pnl_executable: Decimal | None
    return_on_standardized_capital_theoretical: Decimal | None
    return_on_standardized_capital_executable: Decimal | None
    reason_codes: list[str]


class V4T1ScenarioValuationResponse(BaseModel):
    """V4.4A -- GET /v4/experimental/t1-scenario-valuation. A pure
    computation over analytics/decision/v4_t1_pricing.py for ONE
    synthetic leg against caller-supplied inputs -- never a real
    captured quote, never a recommendation, never a rank. EXPERIMENTAL,
    MODEL-BASED T+1 SCENARIOS ONLY -- see that router's own docstring."""

    strategy: str
    entry_cashflow: Decimal | None
    scenarios: list[V4T1ScenarioPointResponse]
    n_scenarios: int
    n_valued: int
    min_return: Decimal | None
    max_return: Decimal | None
    median_return: Decimal | None
    scenario_average_return: Decimal | None
    positive_scenario_fraction: Decimal | None
    quality_note: str
    engine_version: str






class AdminRunResearchPreparationResponse(BaseModel):
    """Pre-live hardening (2026-08-25) -- POST /admin/run-research-
    preparation. This endpoint only ENQUEUES durable ResearchPreparation
    Job rows (services/earnings_research_preparation.py::
    enqueue_preparation_candidates, via services/scheduler.py::
    run_earnings_research_preparation_job) -- it never owns the lifetime
    of the actual (network/CPU-heavy) preparation work, which the
    dedicated research-worker process claims and runs independently of
    this (or any) HTTP request. ``queued``/``already_ready``/
    ``filtered_out``/``preparation_warning`` are exact counts of the real
    per-candidate outcomes from this one call, never fabricated or
    estimated -- ``preparation_warning`` (post-live correction,
    2026-08-25) is a transient, non-blocking eligibility-check failure
    (e.g. a rate-limited options-chain lookup), distinct from a genuine,
    permanent ``filtered_out`` rejection -- see services/earnings_
    eligibility.py::EligibilityResult.retryable's own docstring.
    Preparation only: this endpoint never creates a DecisionSnapshot or
    EntryCaptureAttempt."""

    queued: int
    already_ready: int
    filtered_out: int
    preparation_warning: int


# ---------------------------------------------------------------------------
# Live Operations Monitor -- read-only wire schemas over services/
# operations.py's dataclasses (see that module's own docstring). Every
# field here mirrors a real, already-persisted value or a pure
# computation over one -- never a live IBKR/EarningsAPI/LLM call made
# just to answer a GET request.
# ---------------------------------------------------------------------------


class IbkrHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    gateway_reachable: bool
    authenticated: bool
    connected: bool
    live_account: bool | None
    market_data_quality: str | None
    last_heartbeat_at: datetime | None
    last_error: str | None
    # IBKR TWS Migration, Phase 3 readiness -- see services/operations.py
    # ::IbkrHealth's own comment. "web" for every deployment today.
    provider: str


class EarningsCalendarHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    active_provider: str | None
    fallback_provider: str | None
    last_successful_sync_at: datetime | None
    events_received: int | None
    last_error: str | None
    next_scheduled_sync_at: datetime | None


class AiProviderHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    provider: str
    configured: bool
    last_successful_generation_at: datetime | None
    last_error: str | None
    decision_view_model: str | None = None
    decision_view_thinking: str | None = None
    decision_view_reasoning_effort: str | None = None
    decision_view_max_tokens: int | None = None
    decision_view_config_error: str | None = None


class SchedulerHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    running: bool
    registered_job_count: int
    last_activity_at: datetime | None
    next_activity_at: datetime | None


class DatabaseHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    backend_healthy: bool
    database_healthy: bool
    migration_head: str | None










class SchedulerJobViewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    enabled: bool
    last_run_at: datetime | None
    last_run_status: str | None
    duration_ms: int | None
    items_evaluated: int | None
    items_succeeded: int | None
    items_failed: int | None
    next_run_time: datetime | None
    last_error: str | None


















class OperationsJobsResponse(BaseModel):
    jobs: list[SchedulerJobViewResponse]




class PreparationProgressResponse(BaseModel):
    """Pre-live hardening (2026-08-25) -- GET /operations/preparation-
    progress. Real, live state of the durable research-preparation queue
    (services/operations.py::get_preparation_progress) -- ``queue_depth``/
    ``completed``/``failed`` are always real, current counts (there is no
    "running" scheduler job to gate on any more: enqueueing itself is
    near-instant, the real work happens continuously in the dedicated
    research-worker process). ``worker_active=False`` (every current_*/
    step_*/heartbeat/elapsed field null) is the honest answer whenever no
    row is currently claimed, never a stale leftover from the last one."""

    model_config = ConfigDict(from_attributes=True)

    queue_depth: int
    completed: int
    failed: int
    worker_active: bool
    current_symbol: str | None
    current_stage: str | None
    step_index: int | None
    step_total: int | None
    attempt: int | None
    heartbeat_seconds_ago: float | None
    elapsed_seconds: float | None










class EntryLegRowResponse(BaseModel):
    """Phase 4 forward-test evaluation dataset (2026-08-26), Section 32."""

    model_config = ConfigDict(from_attributes=True)

    leg_index: int
    option_type: str | None
    strike: Decimal | None
    action: str | None
    bid: Decimal | None
    ask: Decimal | None
    benchmark_entry_price: Decimal | None
    pricing_assumption: str | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    market_data_quality: str | None


class ForwardTestDatasetRowResponse(BaseModel):
    """GET /forward-test-dataset -- a canonical, READ-ONLY view over the
    existing official evidence, built for future evaluation/modeling
    work (Section 32-33). No invented data: every field is either read
    directly off an already-frozen row or a pure derivation over one.
    This is infrastructure, not a trading-performance claim -- see
    Section 34's own explicit deferral of any model training."""

    model_config = ConfigDict(from_attributes=True)

    decision_snapshot_id: int
    ticker: str
    generated_at: datetime

    direction: str
    volatility_view: str | None
    effective_risk_profile: str | None
    strategy_type: str | None
    selected_expiration: date | None
    dte_at_generation: int | None
    legs: list | None
    implied_volatility: Decimal | None
    volatility_regime: str | None
    score_breakdown: dict | None
    strategy_score: int | None
    deterministic_confidence_score: int | None
    historical_compatibility: dict | None
    historical_sample_size: int | None
    confidence_interval: dict | None

    entry_status: str | None
    entry_underlying_price: Decimal | None
    entry_net_price_per_share: Decimal | None
    entry_capital_at_risk: Decimal | None
    entry_legs: list[EntryLegRowResponse] | None
    entry_market_data_quality_label: str | None

    settlement_status: str | None
    exit_underlying_price: Decimal | None
    realized_pnl: Decimal | None
    return_pct: Decimal | None
    r_multiple: Decimal | None
    is_win: bool | None
    settlement_market_data_quality_label: str | None

    underlying_move_pct: Decimal | None
    directional_correctness: bool | None
    breakeven_held: bool | None


class ForwardTestDatasetResponse(BaseModel):
    rows: list[ForwardTestDatasetRowResponse]


# ---------------------------------------------------------------------------
# Live Operations (V4-only reset, 2026-09-02)
# ---------------------------------------------------------------------------


class V4ForwardHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    enabled: bool
    decisions_today: int
    ranked_today: int
    no_action_today: int
    failed_today: int
    entry_observations_failed_today: int
    settlements_due: int
    settlements_complete: int
    last_run_at: datetime | None
    engine_version: str | None
    decision_time_et: str
    settlement_time_et: str
    timing_policy_version: str
    note: str


class SystemHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ibkr: IbkrHealthResponse
    earnings_calendar: EarningsCalendarHealthResponse
    ai_provider: AiProviderHealthResponse
    scheduler: SchedulerHealthResponse
    database: DatabaseHealthResponse
    v4_shadow: V4ForwardHealthResponse | None = None


class TimelineStepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    label: str
    at: datetime | None
    status: str
    detail: str | None = None


class V4PipelineEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    calendar_event_id: int
    symbol: str
    company_name: str
    market_cap: str | None
    earnings_date: str
    earnings_timing: str
    entry_timestamp: datetime
    exit_timestamp: datetime
    lifecycle_state: str
    lifecycle_reason: str | None
    next_action: str | None
    next_action_at: datetime | None
    research_ready: bool
    shadow_decision_id: int | None
    decision_status: str | None
    entries_observed: int
    entries_failed: int
    settlements_settled: int
    settlements_failed: int
    timeline: list[TimelineStepResponse]


class ResearchReadinessResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    window_days: int
    upcoming_events: int
    business_eligible: int
    company_resolved: int
    research_queued: int
    research_running: int
    research_ready: int
    research_failed: int
    ai_thesis_ready: int
    v4_decision_ready: int
    next_window_at: datetime | None
    next_window_ready: int
    next_window_total: int


class V4TodaySummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    decision_window_et: str
    settlement_window_et: str
    deadline_et: str
    events_in_window: int
    business_eligible: int
    research_ready: int
    waiting_decision: int
    decisions_today: int
    ranked_today: int
    no_action_today: int
    entries_observed_today: int
    entries_failed_today: int
    deadline_skipped_today: int
    research_not_ready_today: int
    settlements_due_today: int
    settled_today: int
    settlements_failed_today: int


class FailureEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    occurred_at: datetime
    symbol: str | None
    stage: str
    category: str
    explanation: str
    detail: str | None
    retryability: str


class JobStalenessResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    state: str
    last_expected_at: datetime | None
    last_actual_at: datetime | None
    next_run_at: datetime | None
    detail: str


class PreflightCheckResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    label: str
    passed: bool
    detail: str | None = None


class PreflightReadinessResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    checks: list[PreflightCheckResponse]
    ready: bool
    blockers: list[str]


class MarketClockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    utc_now: datetime
    new_york_now: datetime
    zurich_now: datetime
    market_session: str
    next_automatic_action_job_id: str | None
    next_automatic_action_at: datetime | None
    settlement_window_tolerance_minutes: int = 5


class OperationsSummaryResponse(BaseModel):
    """GET /operations/summary -- health, today's V4 summary, research
    readiness, pre-flight and the clock in one aggregated response."""

    health: SystemHealthResponse
    today: V4TodaySummaryResponse
    readiness: ResearchReadinessResponse
    preflight: PreflightReadinessResponse
    market_clock: MarketClockResponse
    staleness: list[JobStalenessResponse] = []


class OperationsEventsResponse(BaseModel):
    events: list[V4PipelineEventResponse]


class OperationsFailuresResponse(BaseModel):
    failures: list[FailureEntryResponse]
