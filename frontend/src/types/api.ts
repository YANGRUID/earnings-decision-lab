// Mirrors backend/src/schemas/api.py — kept manually in sync (no codegen
// pipeline yet; see docs/limitations.md).

export interface Company {
  ticker: string;
  name: string;
  sector: string | null;
  exchange: string | null;
}

export interface EarningsEstimate {
  fiscal_period_end_date: string;
  horizon: string;
  estimated_report_date: string | null;
  date_source: string;
  eps_estimate_average: string | null;
  eps_estimate_high: string | null;
  eps_estimate_low: string | null;
  eps_estimate_analyst_count: number | null;
  eps_revision_direction: string;
  revenue_estimate_average: string | null;
  revenue_estimate_high: string | null;
  revenue_estimate_low: string | null;
  revenue_estimate_analyst_count: number | null;
  revenue_revision_direction: string;
  snapshot_timestamp: string;
  source_provider: string;
}

export interface VolatilitySnapshot {
  method: string;
  target_earnings_date: string | null;
  anchor: string;
  near_term_expiration: string | null;
  next_term_expiration: string | null;
  atm_iv_near: string | null;
  atm_iv_next: string | null;
  term_structure_slope: string | null;
  implied_move_pct: string | null;
  implied_move_absolute: string | null;
  put_call_open_interest_ratio: string | null;
  put_call_volume_ratio: string | null;
  inputs: Record<string, unknown> | null;
  snapshot_timestamp: string;
  computed_at: string;
}

export interface HistoricalMoveStats {
  sample_size: number;
  average_abs_move_pct: string;
  median_abs_move_pct: string;
  largest_abs_move_pct: string;
  largest_move_pct_signed: string;
}

export interface Citation {
  marker: string;
  ticker: string;
  filing_type: string;
  filing_date: string;
  section: string | null;
  source_url: string;
}

export interface FilingSearchResponse {
  context_text: string;
  citations: Citation[];
}

export interface ToolCall {
  tool_name: string;
  arguments: Record<string, unknown>;
  success: boolean;
  duration_ms: number;
  summary: string;
  error: string | null;
  query_description: string | null;
}

export interface ExecutionTrace {
  intent_category: string;
  planning_method: string;
  tool_calls: ToolCall[];
  verification_ran: boolean;
  verification_supported: boolean | null;
  revised: boolean;
  model: string;
  total_input_tokens: number;
  total_output_tokens: number;
  estimated_cost_usd: string | null;
  total_duration_ms: number;
}

// Part A11 (2026-08-26) -- an honest, typed state instead of ever
// hallucinating an answer the backend doesn't actually have. "completed"
// is the only status that carries a real answer/citations/trace; every
// other status means exactly what its name says and nothing was
// fabricated to fill the gap.
export type ResearchQueryStatus =
  | "completed"
  | "preparing"
  | "insufficient_evidence"
  | "company_not_found"
  | "research_failed";

// One company whose research isn't ready yet -- already enqueued (or
// already in flight) on the same durable queue the automated scheduler
// uses, never prepared synchronously inside the request.
export interface PreparingCompany {
  ticker: string;
  job_id: number;
  job_status: string;
}

export interface ResearchQueryResponse {
  question: string;
  status: ResearchQueryStatus;
  answer: string | null;
  citations: Citation[];
  trace: ExecutionTrace | null;
  preparing: PreparingCompany[];
  unresolved_tickers: string[];
}

// A real, persisted AI Research answer -- see models/ai_research_query.py.
// Selecting one from history restores exactly this stored answer; it is
// never regenerated. The active-answer panel in Research.tsx renders one
// of these regardless of whether it was just generated or pulled from
// history, so the two code paths never drift apart.
export interface AIResearchHistoryItem {
  id: number;
  ticker: string | null;
  question: string;
  answer_markdown: string;
  citations: Citation[];
  intent_category: string;
  planning_method: string;
  tool_calls: ToolCall[];
  verification_ran: boolean;
  verification_supported: boolean | null;
  revised: boolean;
  provider: string;
  model: string;
  total_input_tokens: number;
  total_output_tokens: number;
  estimated_cost_usd: string | null;
  total_duration_ms: number;
  created_at: string;
}

// A real, persisted AI Earnings Thesis generation -- see
// models/ai_thesis_version.py. One row per generation, never overwritten.
export interface AIThesisVersion {
  id: number;
  company_id: number;
  business_context: string;
  historical_earnings_pattern: string;
  guidance_trend: string;
  key_risks: string;
  market_setup: string;
  disclaimer: string;
  citations: Citation[];
  provider: string;
  model: string;
  earnings_estimate_snapshot_id: number | null;
  volatility_snapshot_id: number | null;
  created_at: string;
  is_stale: boolean;
}

export interface RetrievalSummary {
  item_count: number;
  mean_recall_at_3: number;
  mean_recall_at_5: number;
  mean_recall_at_10: number;
  mean_mrr: number;
}

export interface RagAnswerSummary {
  item_count: number;
  mean_fact_coverage: number;
  fully_correct_count: number;
  mean_citation_precision: number;
  mean_citation_completeness: number;
  mean_duration_ms: number;
}

export interface AgentSummary {
  item_count: number;
  intent_accuracy: number;
  tool_selection_accuracy: number;
  verification_run_rate: number;
  mean_duration_ms: number;
  total_estimated_cost_usd: number;
}

export interface ExtractionSummary {
  item_count: number;
  non_capex_null_accuracy: number;
  capex_accuracy: number;
  tone_plausibility_rate: number;
  keyword_hit_rate: number;
}

export interface EvaluationRun {
  run_at: string;
  llm_provider: string;
  llm_model: string;
  embedding_model: string;
  retrieval: RetrievalSummary | null;
  rag_answer: RagAnswerSummary | null;
  agent: AgentSummary | null;
  extraction: ExtractionSummary | null;
}

export interface EvaluationStatusResponse {
  available: boolean;
  run: EvaluationRun | null;
}

export interface DataCounts {
  companies: number;
  earnings_events: number;
  earnings_events_with_results: number;
  price_bars: number;
  filings: number;
  document_chunks: number;
  earnings_estimate_snapshots: number;
  options_snapshots: number;
  volatility_snapshots: number;
}

export interface DataFreshness {
  latest_price_bar_date: string | null;
  latest_filing_retrieved_at: string | null;
  latest_earnings_estimate_snapshot_at: string | null;
  latest_options_snapshot_at: string | null;
}

export interface LlmConfigStatus {
  provider: string;
  model: string | null;
  configured: boolean;
}

export interface IbkrStatus {
  gateway_reachable: boolean;
  authenticated: boolean;
  connected: boolean;
  competing: boolean;
  error: string | null;
  // Phase 4.8A -- the short label services/system_status.py::ibkr_status_label
  // computes from the fields above: "CONNECTED" | "AUTH_REQUIRED" |
  // "COMPETING_SESSION" | "GATEWAY_UNREACHABLE".
  status_label: string;
}

export interface IbkrConnectResponse {
  url: string;
}

// IBKR TWS Migration Phase 1 -- the additive TWS-transport sibling of
// IbkrStatus above (that one, the existing Web/Client-Portal-Gateway
// status, is unchanged by this migration). See services/system_status.py
// ::get_tws_status / schemas.api.TwsStatusResponse.
export interface TwsStatus {
  configured: boolean;
  gateway_reachable: boolean;
  socket_connected: boolean;
  api_ready: boolean;
  market_data_quality: string | null;
  error: string | null;
  // "NOT_CONFIGURED" | "GATEWAY_UNREACHABLE" | "AUTH_REQUIRED" | "CONNECTED"
  status_label: string;
  // IBKR TWS Migration, Phase 3 readiness -- additive. reconnect_state is
  // TWSConnectionState's own real value ("disconnected" | "connecting" |
  // "connected" | "ready" | "reconnecting" | "failed"), more granular
  // than status_label above.
  last_heartbeat: string | null;
  reconnect_state: string;
}

export interface ProviderCapabilities {
  prices: boolean;
  earnings_estimates: boolean;
  earnings_calendar: boolean;
  filings: boolean;
  options: boolean;
  greeks: boolean;
  ai: boolean;
}

export interface ProviderStatus {
  provider: string;
  domain: string;
  configured: boolean;
  masked_key: string | null;
  last_success_at: string | null;
  last_error_at: string | null;
  last_error_status: string | null;
  last_error_detail: string | null;
  entitlement_note: string | null;
  capabilities: ProviderCapabilities;
}

export interface DomainStatus {
  domain: string;
  primary: string | null;
  fallback: string | null;
  primary_is_override: boolean;
  fallback_is_override: boolean;
  providers: ProviderStatus[];
}

export interface ProviderDashboard {
  v4_decision_view?: {
    provider: string | null;
    model: string | null;
    thinking: string | null;
    reasoning_effort: string | null;
    max_tokens: number | null;
    config_version: string | null;
    config_error: string | null;
  } | null;
  domains: DomainStatus[];
  strategy_risk_preference: string;
}

export interface ProviderSettingsUpdate {
  price_history_primary?: string | null;
  price_history_fallback?: string | null;
  clear_price_history_fallback?: boolean;
  options_primary?: string | null;
  options_fallback?: string | null;
  clear_options_fallback?: boolean;
  llm_provider?: string | null;
  llm_model?: string | null;
  strategy_risk_preference?: string | null;
}

export interface ProviderUsageSummary {
  provider: string;
  domain: string;
  request_count: number;
  success_count: number;
  error_count: number;
  rate_limited_count: number;
  avg_latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  estimated_cost: string | null;
  last_event_at: string | null;
}

export interface UsageSummary {
  window: string;
  since: string | null;
  total_requests: number;
  total_errors: number;
  total_rate_limited: number;
  total_llm_tokens: number | null;
  total_estimated_cost: string | null;
  providers: ProviderUsageSummary[];
}

export interface TestConnectionResult {
  provider: string;
  domain: string;
  status: string;
  detail: string | null;
  tested_at: string;
}

export interface SystemStatus {
  counts: DataCounts;
  freshness: DataFreshness;
  llm: LlmConfigStatus;
  embedding_model: string;
  evaluation: EvaluationStatusResponse;
  ibkr: IbkrStatus;
  tws: TwsStatus;
  market_session: string;
  providers: ProviderDashboard;
}

export interface PreparationStep {
  step: string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  detail: string | null;
  updated_at: string;
  retryable: boolean | null;
}

export interface ResearchJob {
  id: number;
  ticker: string;
  company_id: number | null;
  status: "running" | "completed" | "completed_with_warnings" | "failed";
  steps: PreparationStep[];
  started_at: string;
  completed_at: string | null;
  error: string | null;
}

export interface ResearchJobQueued {
  ticker: string;
  status: "queued";
}

// The one canonical options-availability answer -- Dashboard, Company
// Overview, Upcoming Earnings, and Strategy Lab all render this same
// object rather than each inferring availability from a different signal.
export interface OptionsMarketState {
  chain_exists: boolean;
  contract_count: number;
  priceable_contract_count: number;
  has_bid_ask: boolean;
  has_iv: boolean;
  has_greeks: boolean;
  bid_ask_contract_count: number;
  iv_contract_count: number;
  greeks_contract_count: number;
  volume_coverage: number;
  oi_coverage: number;
  implied_move_available: boolean;
  earnings_anchored: boolean | null;
  expiration: string | null;
  source: string | null;
  snapshot_timestamp: string | null;
  snapshot_age_minutes: number | null;
  snapshot_age_label: string | null;
  market_data_quality: string | null;
  data_state: string;
  reason: string;
  snapshot_tier: "current_priceable" | "previous_priceable" | "contracts_only" | "none";
  is_fallback_snapshot: boolean;
  snapshot_purpose: string | null;
  actionability:
    | "actionable_current"
    | "actionable_previous_session"
    | "stale_research_only"
    | "contracts_only"
    | "unavailable";
  underlying_price: string | null;
  underlying_timestamp: string | null;
}

// The single, company-level, always-current read for "what's on record
// right now" -- this is where forward-looking data (next-period estimate,
// implied move) actually belongs, never on a specific past EarningsEvent.
export interface ResearchOverview {
  ticker: string;
  company: Company | null;
  latest_job: ResearchJob | null;
  earnings_events_count: number;
  price_bars_count: number;
  filings_count: number;
  filing_chunks_count: number;
  latest_earnings_estimate: EarningsEstimate | null;
  latest_volatility_snapshot: VolatilitySnapshot | null;
  latest_price: string | null;
  historical_moves: HistoricalMoveStats | null;
  options_market: OptionsMarketState;
}

export interface ResearchOverviewListResponse {
  overviews: ResearchOverview[];
}

export interface OptionLeg {
  option_type: "call" | "put";
  action: "buy" | "sell";
  strike: string;
  premium: string;
  quantity: number;
}

export interface StrategyAnalysis {
  net_premium: string;
  max_profit: string | null;
  max_loss: string | null;
  breakevens: string[];
  return_on_risk: string | null;
}

export interface MoveCompatibility {
  method: string;
  sample_size: number;
  requires_move_beyond_threshold: boolean;
  required_move_pct: string;
  compatible_count: number;
  compatible_pct: string;
}

export interface EarningsThesis {
  business_context: string;
  historical_earnings_pattern: string;
  guidance_trend: string;
  key_risks: string;
  market_setup: string;
  disclaimer: string;
  citations: Citation[];
  generated_at: string;
  model: string;
}

export interface ApiError {
  error: string;
  request_id: string | null;
}

// --- AI Options Decision Engine (Phase 14.9) ---

export type DecisionDirection =
  | "strong_bullish"
  | "bullish"
  | "neutral"
  | "bearish"
  | "strong_bearish";
export type DecisionVolatilityView = "long_vol" | "neutral_vol" | "short_vol";

export interface BudgetFit {
  trade_budget: string;
  risk_cap: string | null;
  usable_risk_budget: string;
  capital_at_risk_per_contract: string | null;
  max_feasible_quantity: number;
  total_max_loss: string | null;
  total_max_profit: string | null;
  total_net_premium: string | null;
  budget_utilization_pct: string | null;
  remaining_budget: string | null;
  feasible: boolean;
  minimum_required: string | null;
}

export interface ScoredStrategy {
  category: string;
  legs: OptionLeg[];
  analysis: StrategyAnalysis;
  score: number;
  score_components: Record<string, number>;
  why: string[];
  risks: string[];
  target_price: string | null;
  payoff_at_target: string | null;
  budget_fit: BudgetFit | null;
  why_expiration?: string[];
  why_strikes?: string[];
  why_risk_profile?: string[];
  why_not_alternative?: string[];
}

export interface MoveCompatibility {
  method: string;
  sample_size: number;
  requires_move_beyond_threshold: boolean;
  required_move_pct: string;
  compatible_count: number;
  compatible_pct: string;
}

export interface EstimatedProbability {
  method: string;
  sample_size: number;
  compatible_count: number;
  probability: string;
  low_sample_confidence: boolean;
  wilson_lower: string | null;
  wilson_upper: string | null;
}

export type RiskProfile = "conservative" | "moderate" | "aggressive";

export interface AIDecisionVersion {
  id: number;
  company_id: number;
  direction: DecisionDirection;
  volatility_view: DecisionVolatilityView;
  confidence_score: number;
  confidence_components: Record<string, number>;
  rationale: string;
  bull_case: string;
  bear_case: string;
  key_catalysts: string;
  key_risks: string;
  disclaimer: string;
  citations: Citation[];
  decision_source: "ai" | "manual_override";
  risk_preference: string;
  risk_profile: RiskProfile | null;
  recommended_strategy_category: string | null;
  recommended_strategy_legs: OptionLeg[] | null;
  recommended_strategy_analysis: StrategyAnalysis | null;
  recommended_strategy_score: number | null;
  recommended_strategy_score_components: Record<string, number> | null;
  recommended_strategy_why: string[] | null;
  recommended_strategy_risks: string[] | null;
  recommended_strategy_why_expiration: string[] | null;
  recommended_strategy_why_strikes: string[] | null;
  recommended_strategy_why_risk_profile: string[] | null;
  recommended_strategy_why_not_alternative: string[] | null;
  historical_compatibility: MoveCompatibility | null;
  estimated_probability: EstimatedProbability | null;
  alternative_strategies: ScoredStrategy[] | null;
  expiration: string | null;
  underlying_price: string | null;
  implied_move_pct: string | null;
  provider: string;
  model: string;
  earnings_estimate_snapshot_id: number | null;
  volatility_snapshot_id: number | null;
  trade_budget: string | null;
  risk_cap: string | null;
  risk_cap_is_percent: boolean | null;
  recommended_quantity: number | null;
  recommended_capital_at_risk: string | null;
  budget_infeasible_minimum: string | null;
  no_market_data_reason: string | null;
  status: "open" | "settled" | "void";
  is_final: boolean;
  earnings_event_id: number | null;
  actual_next_day_move_pct: string | null;
  actual_five_day_move_pct: string | null;
  direction_correct: boolean | null;
  actual_move_exceeded_implied: boolean | null;
  breakeven_met: boolean | null;
  strategy_pnl: string | null;
  strategy_pnl_available: boolean;
  settled_at: string | null;
  created_at: string;
  settlement_eligible: boolean;
  settlement_state:
    | "not_final"
    | "earnings_date_unknown"
    | "waiting_for_event"
    | "waiting_for_post_event_price"
    | "ready"
    | "settled";
  settlement_reason: string;
  settlement_earliest_date: string | null;
}

export interface Rate {
  correct: number;
  total: number;
  pct: string | null;
}

// --- AI Earnings Analyst Dashboard (product frontend layer) --------------
// Reads the same immutable Phase 4 tables the Track Record analytics
// above already read (DecisionSnapshot/EntrySnapshot/EntryCaptureAttempt/
// V4 settlement rows), never any on-demand journal
// journal.

export interface EarningsCalendarEvent {
  id: number;
  symbol: string;
  company_name: string;
  logo_url: string | null;
  earnings_date: string;
  earnings_time: "bmo" | "amc" | "dmh" | "unknown";
  eps_estimate: string | null;
  revenue_estimate: string | null;
  market_cap: string | null;
  source: string;
}

export interface DecisionSnapshot {
  id: number;
  earnings_calendar_event_id: number;
  benchmark_portfolio_id: number;
  ticker: string;
  company_name: string;
  strategy_direction: DecisionDirection;
  strategy_type: string | null;
  ai_thesis_version_id: number | null;
  generated_at: string;
  status: string;

  underlying_price: string | null;
  implied_volatility: string | null;
  volatility_regime: string | null;
  option_snapshot_reference: number | null;

  strategy_score: number | null;
  score_breakdown: Record<string, number> | null;
  selected_expiration: string | null;
  legs: OptionLeg[] | null;

  estimated_probability: string | null;
  // Phase 4 decision-communication hardening (2026-08-26), Section 29 --
  // fixed to match what services/decision_snapshot_freezing.py actually
  // populates (wilson_lower/wilson_upper/low_sample_confidence), not the
  // { lower, upper } shape this previously, incorrectly, declared.
  confidence_interval: {
    wilson_lower: string | null;
    wilson_upper: string | null;
    low_sample_confidence: boolean;
  } | null;
  historical_sample_size: number | null;
  historical_compatibility: Record<string, unknown> | null;

  // Phase 4 reproducibility hardening (2026-08-26), Sections 2-6 -- null
  // on every historical (including every Aug 25) row.
  volatility_view: "long_vol" | "neutral_vol" | "short_vol" | null;
  effective_risk_profile: "conservative" | "moderate" | "aggressive" | null;
  // EVIDENCE STRENGTH (analytics/decision/confidence.py) -- how much
  // real evidence backs this view, never probability of profit,
  // strategy score, or LLM self-reported confidence.
  deterministic_confidence_score: number | null;
  deterministic_confidence_breakdown: Record<string, number> | null;
  decision_llm_provider: string | null;
  decision_llm_model: string | null;

  why_this_strategy: string[] | null;
  why_this_expiration: string[] | null;
  why_these_strikes: string[] | null;
  why_not_alternatives: string[] | null;

  engine_version: string;
  prompt_version: string;
  expiration_source: string;
  created_at: string;
}

export interface EntrySnapshot {
  id: number;
  capture_attempt_id: number;
  leg_index: number;
  status: string;
  captured_at: string | null;

  external_contract_id: string | null;
  expiration: string | null;
  strike: string | null;
  option_type: "call" | "put" | null;
  action: "buy" | "sell" | null;
  quantity: number | null;
  multiplier: string | null;

  bid: string | null;
  ask: string | null;
  mid: string | null;
  last_price: string | null;
  implied_volatility: string | null;
  delta: string | null;
  gamma: string | null;
  theta: string | null;
  vega: string | null;
  market_data_quality: string | null;
  pricing_source: string | null;

  benchmark_entry_price: string | null;
  pricing_assumption: string | null;

  capture_error: string | null;
  source_provider: string | null;
  created_at: string;
}

export interface EntryCaptureAttempt {
  id: number;
  decision_snapshot_id: number;
  benchmark_portfolio_id: number;
  status: string;
  capture_error: string | null;

  underlying_price: string | null;
  underlying_bid: string | null;
  underlying_ask: string | null;
  underlying_timestamp: string | null;
  option_market_timestamp: string | null;

  net_entry_price_per_share: string | null;
  net_entry_cash: string | null;
  contracts: number | null;
  initial_max_risk: string | null;
  capital_utilization: string | null;

  source_provider: string | null;
  captured_at: string | null;
  created_at: string;
  // Phase 4 market-data-quality hardening (2026-08-26), Section 17.
  market_data_quality_label: "VERIFIED_LIVE" | "DELAYED_DATA" | "UNKNOWN_QUALITY";

  legs: EntrySnapshot[];
}

export interface ExitSnapshot {
  id: number;
  settlement_attempt_id: number;
  entry_snapshot_id: number;
  leg_index: number;
  status: string;
  captured_at: string | null;

  external_contract_id: string | null;
  expiration: string | null;
  strike: string | null;
  option_type: "call" | "put" | null;
  action: "buy" | "sell" | null;
  quantity: number | null;
  multiplier: string | null;

  bid: string | null;
  ask: string | null;
  mid: string | null;
  last_price: string | null;
  implied_volatility: string | null;
  delta: string | null;
  gamma: string | null;
  theta: string | null;
  vega: string | null;
  market_data_quality: string | null;
  pricing_source: string | null;

  benchmark_exit_price: string | null;
  pricing_assumption: string | null;
  realized_pnl_per_share: string | null;

  capture_error: string | null;
  source_provider: string | null;
  created_at: string;
}

export interface SettlementCaptureAttempt {
  id: number;
  decision_snapshot_id: number;
  benchmark_portfolio_id: number;
  entry_capture_attempt_id: number | null;
  status: string;
  capture_error: string | null;

  underlying_price: string | null;
  underlying_bid: string | null;
  underlying_ask: string | null;
  underlying_timestamp: string | null;
  exit_market_timestamp: string | null;

  net_exit_price_per_share: string | null;
  net_exit_cash: string | null;
  realized_pnl: string | null;
  return_pct: string | null;
  r_multiple: string | null;
  is_win: boolean | null;

  source_provider: string | null;
  captured_at: string | null;
  created_at: string;
  market_data_quality_label: "VERIFIED_LIVE" | "DELAYED_DATA" | "UNKNOWN_QUALITY";

  legs: ExitSnapshot[];
}

/** Derived, client-side only -- never returned by any endpoint as a
 * literal field. Mirrors backend services/decision_lifecycle.py's own
 * three stages exactly, computed here from the same real facts (a
 * CAPTURED EntryCaptureAttempt / SettlementCaptureAttempt existing for
 * the decision) rather than duplicating any calculation. */
// ---------------------------------------------------------------------
// Live Operations Monitor -- read-only, mirrors backend schemas/api.py's
// operations response models field-for-field. Every value here is a
// real, already-persisted row or a pure computation over one -- see
// backend services/operations.py's own module docstring.
// ---------------------------------------------------------------------

export type HealthState = "green" | "yellow" | "red" | "gray";

export interface IbkrHealth {
  state: HealthState;
  gateway_reachable: boolean;
  authenticated: boolean;
  connected: boolean;
  live_account: boolean | null;
  market_data_quality: string | null;
  last_heartbeat_at: string | null;
  last_error: string | null;
  // IBKR TWS Migration, Phase 3 readiness -- the backend has sent this
  // ("web" | "tws") since Phase 3's own Section 13; this type was simply
  // never updated to match, so Operations couldn't render it.
  provider: string;
}

export interface EarningsCalendarHealth {
  state: HealthState;
  active_provider: string | null;
  fallback_provider: string | null;
  last_successful_sync_at: string | null;
  events_received: number | null;
  last_error: string | null;
  next_scheduled_sync_at: string | null;
}

export interface AiProviderHealth {
  state: HealthState;
  provider: string;
  configured: boolean;
  last_successful_generation_at: string | null;
  last_error: string | null;
  // The explicit V4 DecisionView model configuration (2026-09-02).
  decision_view_model?: string | null;
  decision_view_thinking?: string | null;
  decision_view_reasoning_effort?: string | null;
  decision_view_max_tokens?: number | null;
  decision_view_config_error?: string | null;
}

export interface SchedulerHealth {
  state: HealthState;
  running: boolean;
  registered_job_count: number;
  last_activity_at: string | null;
  next_activity_at: string | null;
}

export interface DatabaseHealth {
  state: HealthState;
  backend_healthy: boolean;
  database_healthy: boolean;
  migration_head: string | null;
}

export interface V4ForwardHealth {
  state: HealthState;
  enabled: boolean;
  decisions_today: number;
  ranked_today: number;
  no_action_today: number;
  failed_today: number;
  entry_observations_failed_today: number;
  settlements_due: number;
  settlements_complete: number;
  last_run_at: string | null;
  engine_version: string | null;
  decision_time_et: string;
  settlement_time_et: string;
  timing_policy_version: string;
  note: string;
}

export interface SystemHealth {
  ibkr: IbkrHealth;
  earnings_calendar: EarningsCalendarHealth;
  ai_provider: AiProviderHealth;
  scheduler: SchedulerHealth;
  database: DatabaseHealth;
  v4_shadow: V4ForwardHealth | null;
}

export interface TimelineStep {
  label: string;
  at: string | null;
  status: "done" | "pending" | "failed" | "warning";
  detail: string | null;
}

// V4-only reset (2026-09-02): one row per calendar event in the V4 pipeline
// window, classified by the backend state machine (services/operations.py).
export type V4PipelineState =
  | "CALENDAR_DISCOVERED"
  | "BUSINESS_INELIGIBLE"
  | "COMPANY_RESOLUTION_FAILED"
  | "RESEARCH_QUEUED"
  | "RESEARCH_RUNNING"
  | "RESEARCH_READY"
  | "RESEARCH_FAILED"
  | "RESEARCH_NOT_READY"
  | "WAITING_DECISION"
  | "DECISION_WINDOW_MISSED"
  | "DECISION_FAILED"
  | "DEADLINE_SKIPPED"
  | "NO_ACTION"
  | "ENTRY_OBSERVED"
  | "ENTRY_FAILED"
  | "WAITING_SETTLEMENT"
  | "SETTLED"
  | "SETTLEMENT_FAILED";

export interface PipelineEvent {
  calendar_event_id: number;
  symbol: string;
  company_name: string;
  market_cap: string | null;
  earnings_date: string;
  earnings_timing: "bmo" | "amc" | "dmh" | "unknown";
  entry_timestamp: string;
  exit_timestamp: string;
  lifecycle_state: V4PipelineState | string;
  lifecycle_reason: string | null;
  next_action: string | null;
  next_action_at: string | null;
  research_ready: boolean;
  shadow_decision_id: number | null;
  decision_status: string | null;
  entries_observed: number;
  entries_failed: number;
  settlements_settled: number;
  settlements_failed: number;
  timeline: TimelineStep[];
}

export interface SchedulerJobView {
  job_id: string;
  enabled: boolean;
  last_run_at: string | null;
  last_run_status: string | null;
  duration_ms: number | null;
  items_evaluated: number | null;
  items_succeeded: number | null;
  items_failed: number | null;
  next_run_time: string | null;
  last_error: string | null;
}

export interface FailureEntry {
  occurred_at: string;
  symbol: string | null;
  stage: string;
  category: string;
  explanation: string;
  detail: string | null;
  retryability: "RETRYABLE" | "NOT_RETRYABLE" | "WINDOW_MISSED";
}

export interface PreflightCheck {
  label: string;
  passed: boolean;
  detail: string | null;
}

export interface PreflightReadiness {
  checks: PreflightCheck[];
  ready: boolean;
  blockers: string[];
}

export interface MarketClock {
  utc_now: string;
  new_york_now: string;
  zurich_now: string;
  market_session: "pre_market" | "regular" | "after_hours" | "closed";
  next_automatic_action_job_id: string | null;
  next_automatic_action_at: string | null;
  settlement_window_tolerance_minutes: number;
}

export interface ResearchReadiness {
  window_days: number;
  upcoming_events: number;
  business_eligible: number;
  company_resolved: number;
  research_queued: number;
  research_running: number;
  research_ready: number;
  research_failed: number;
  ai_thesis_ready: number;
  v4_decision_ready: number;
  next_window_at: string | null;
  next_window_ready: number;
  next_window_total: number;
}

export interface V4TodaySummary {
  decision_window_et: string;
  settlement_window_et: string;
  deadline_et: string;
  events_in_window: number;
  business_eligible: number;
  research_ready: number;
  waiting_decision: number;
  decisions_today: number;
  ranked_today: number;
  no_action_today: number;
  entries_observed_today: number;
  entries_failed_today: number;
  deadline_skipped_today: number;
  research_not_ready_today: number;
  settlements_due_today: number;
  settled_today: number;
  settlements_failed_today: number;
}

export interface JobStaleness {
  job_id: string;
  state: "ok" | "stale" | "missed" | "never" | string;
  last_expected_at: string | null;
  last_actual_at: string | null;
  next_run_at: string | null;
  detail: string;
}

// The 15:30 ET forward window (settlement-priority hardening, v4.0.0): what
// the next window will do, in execution order, and how the last one went.
export interface ForwardWindow {
  window_time_et: string;
  priority: string[];
  next_window_at: string | null;
  settlements_due: string[];
  decisions_ready: string[];
  decisions_not_ready: string[];
  last_window_started_at: string | null;
  last_settlements_due: number;
  last_settlements_settled: number;
  last_settlements_failed: number;
  last_settlements_window_missed: number;
  last_settlement_lock_wait_ms_max: number | null;
  last_settlement_total_ms_max: number | null;
  last_decisions_ready: number;
  last_deadline_skipped: number;
  last_decision_lock_wait_ms: number | null;
}

export interface OperationsSummary {
  health: SystemHealth;
  today: V4TodaySummary;
  readiness: ResearchReadiness;
  preflight: PreflightReadiness;
  market_clock: MarketClock;
  staleness: JobStaleness[];
  forward_window?: ForwardWindow | null;
}

export interface OperationsEvents {
  events: PipelineEvent[];
}

export interface OperationsJobs {
  jobs: SchedulerJobView[];
}

export interface OperationsFailures {
  failures: FailureEntry[];
}

export interface PreparationProgress {
  queue_depth: number;
  completed: number;
  failed: number;
  worker_active: boolean;
  current_symbol: string | null;
  current_stage: string | null;
  step_index: number | null;
  step_total: number | null;
  attempt: number | null;
  heartbeat_seconds_ago: number | null;
  elapsed_seconds: number | null;
}

// ---------------------------------------------------------------------
// V4.5 -- EXPERIMENTAL V4 shadow cohort. Deliberately separate types from
// the V4 forward record: these describe analytical observations, not
// forward-test evidence, and the two are never merged.
// ---------------------------------------------------------------------

export interface V4ShadowView {
  direction: string | null;
  volatility: string | null;
  expected_move_intent: string | null;
  confidence: string | null;
  reasoning: string | null;
}

export interface V4ShadowProvenance {
  llm_provider: string | null;
  /** The configured model alias the view was requested from. */
  llm_model: string | null;
  prompt_version: string | null;
  decision_view_schema_version: string | null;
  // Model/reasoning provenance (2026-09-02); absent on views frozen earlier.
  /** What the API itself reported -- may equal the alias, never invented. */
  llm_returned_model?: string | null;
  llm_thinking?: string | null;
  llm_reasoning_effort?: string | null;
  llm_max_tokens?: number | null;
  llm_finish_reason?: string | null;
  llm_input_tokens?: number | null;
  llm_output_tokens?: number | null;
  llm_reasoning_tokens?: number | null;
  llm_cache_hit_tokens?: number | null;
  llm_latency_ms?: number | null;
  llm_config_version?: string | null;
  generated_at?: string | null;
}

export interface V4ShadowMarketData {
  underlying_price: string | null;
  underlying_quote_at: string | null;
  market_data_quality: string | null;
  source_provider: string | null;
  max_input_skew_seconds: string | null;
}

export interface V4ShadowDecisionSummary {
  id: number;
  earnings_calendar_event_id: number;
  ticker: string;
  company_name: string;
  legal_decision_window_at: string;
  generated_at: string;
  as_of: string;
  status: string;
  no_action_reason: string | null;
  failure_category: string | null;
  rank_1_candidate_id: string | null;
  candidate_count: number;
  rankable_candidate_count: number;
  view: V4ShadowView | null;
  provenance: V4ShadowProvenance | null;
  market_data: V4ShadowMarketData | null;
  versions: Record<string, string | null> | null;
  timing_policy_version?: string | null;
  expected_move?: V4ExpectedMove | null;
  notice: string;
}

export type V4ShadowDecisionDetail = V4ShadowDecisionSummary;

export interface V4ShadowCandidateLeg {
  leg_index: number;
  action: string;
  right: string;
  strike: string;
  quantity: number;
  external_contract_id: string | null;
  required_side: string | null;
  required_side_price: string | null;
  bid: string | null;
  ask: string | null;
  implied_volatility: string | null;
  market_data_quality: string | null;
  source_provider: string | null;
  retrieved_at: string | null;
}

export interface V4ShadowCandidate {
  candidate_id: string;
  rank: number | null;
  strategy: string;
  expiration: string;
  geometry_variant_id: string | null;
  validity_status: string;
  status_reason: string | null;
  semantic: { compatibility: string | null; tier: string | null } | null;
  // CORE and TAIL STRESS are separate objects on purpose -- they must
  // never be averaged together.
  core: {
    worst_return: string | null;
    median_return: string | null;
    best_return: string | null;
    positive_scenario_fraction: string | null;
    positive_region_count: number | null;
    region_count: number | null;
    scenarios_valued: number | null;
    no_profitable_region: boolean | null;
    profit_concentrated_in_single_region: boolean | null;
  } | null;
  tail_stress: {
    worst_return: string | null;
    large_move_survival: string | null;
    vs_core_worst_delta: string | null;
    scenarios_valued: number | null;
    note: string;
  } | null;
  execution: {
    mean_relative_spread: string | null;
    worst_relative_spread: string | null;
    two_sided_leg_count: number | null;
    leg_count: number | null;
    required_sides_complete: boolean | null;
    max_leg_timestamp_skew_seconds: string | null;
    market_data_quality: string | null;
  } | null;
  capital: {
    standardized_capital: string | null;
    entry_cash_required: string | null;
    capital_utilisation: string | null;
  } | null;
  rank_explanation: string | null;
  data_quality_warnings?: { warnings: string[] } | null;
  scenario_grid?: V4ScenarioGrid | null;
  legs: V4ShadowCandidateLeg[];
}

export interface V4ShadowDecisionsResponse {
  notice: string;
  decisions: V4ShadowDecisionSummary[];
}

export interface V4ShadowCandidatesResponse {
  notice: string;
  candidates: V4ShadowCandidate[];
}

export interface V4ShadowTrackRecord {
  notice: string;
  cohort: string;
  counts: {
    shadow_decisions: number;
    ranked: number;
    no_action: number;
    failed: number;
    entry_observed: number;
    entry_not_executable: number;
    settled: number;
    settlement_failed: number;
  };
  sample_sufficiency: string;
  minimum_meaningful_sample: number;
  performance_note: string;
}

// ---------------------------------------------------------------------------
// V4 consolidation (2026-09-02) -- six-configuration read models.
// ---------------------------------------------------------------------------
export interface V4ConfigExclusion {
  candidate_id: string;
  reason_code: string;
  detail: string;
}

export interface V4CandidateSummary {
  candidate_id: string;
  unconstrained_rank: number | null;
  strategy: string;
  expiration: string;
  validity_status: string;
  semantic_tier: string | null;
  core_worst_return: string | null;
  core_median_return: string | null;
  core_positive_scenario_fraction: string | null;
  stress_worst_return: string | null;
  mean_relative_spread: string | null;
  entry_cash_required: string | null;
  market_data_quality: string | null;
  rank_explanation: string | null;
}

export interface V4ConfigResult {
  configuration_key: string;
  label: string;
  capital_base: string;
  risk_profile: string;
  configuration_version: string;
  max_risk_dollars: string;
  max_risk_utilization_pct: string;
  status: "RANKED" | "NO_ACTION" | "FAILED" | string;
  no_action_reason: string | null;
  rank_1_candidate_id: string | null;
  eligible_candidate_count: number;
  excluded_candidate_count: number;
  exclusions: V4ConfigExclusion[];
  ranked_candidate_ids: string[];
  ranking_version: string | null;
  rank_1: V4CandidateSummary | null;
  // V4 lifecycle (Sections 23-27). WAITING_ENTRY for a configuration whose
  // rank #1 differs from the event-level observed candidate -- no separate
  // observation stream exists yet, and evidence is never borrowed.
  lifecycle?: V4Lifecycle;
  // Per-configuration forward evidence (activation phase). Never borrowed
  // from another configuration or from the event-level observation.
  entry?: V4ConfigEntry | null;
  settlement?: V4ConfigSettlement | null;
}

export interface V4ObservedLeg {
  leg_index: number; action: string; right: string; strike: string;
  external_contract_id: string | null; required_side: string; price: string | null;
  bid: string | null; ask: string | null; market_data_quality: string | null;
  implied_volatility?: string | null; retrieved_at?: string | null;
}

export interface V4ConfigEntry {
  status: string; candidate_id: string; quantity: number; standardized_capital: string;
  capital_used: string | null; max_risk_per_contract: string | null; max_risk_used: string | null;
  entry_net_value: string | null; pricing_convention: string; observed_at: string;
  market_data_quality: string | null; failure_category: string | null; failure_detail: string | null;
  timing_policy_version: string | null; legs: V4ObservedLeg[] | null;
  earliest_leg_observed_at: string | null; latest_leg_observed_at: string | null;
  max_leg_timestamp_skew_seconds: string | null;
}

export interface V4ConfigSettlement {
  status: string; candidate_id: string; quantity: number; standardized_capital: string;
  capital_used: string | null; entry_net_value: string | null; exit_net_value: string | null;
  realized_pnl: string | null; return_on_standardized_capital: string | null;
  entry_observed_at: string | null; settled_at: string; pricing_convention: string;
  market_data_quality: string | null; failure_category: string | null; failure_detail: string | null;
  legs: V4ObservedLeg[] | null;
}

export type V4Lifecycle =
  | "RANKED" | "NO_ACTION" | "FAILED" | "WAITING_ENTRY" | "ENTRY_OBSERVED"
  | "ENTRY_FAILED" | "WAITING_SETTLEMENT" | "SETTLED" | "SETTLEMENT_FAILED";

export interface V4EntryObservation {
  status: string;
  candidate_id: string;
  observed_at: string | null;
  failure_category: string | null;
  failure_detail: string | null;
  market_data_quality: string | null;
  net_executable_value: string | null;
}

export interface V4SettlementOutcome {
  status: string;
  settled_at: string | null;
  failure_category: string | null;
  failure_detail: string | null;
  entry_net_value: string | null;
  exit_net_value: string | null;
  realized_pnl: string | null;
  return_on_standardized_capital: string | null;
  market_data_quality: string | null;
}

export interface V4ShadowConfigurationsResponse {
  notice: string;
  decision: V4ShadowDecisionSummary & {
    expected_move?: V4ExpectedMove | null;
  };
  timing_policy_version: string | null;
  configurations: V4ConfigResult[];
  candidates: V4CandidateSummary[];
  default_configuration_key: string;
  entry_observation?: V4EntryObservation | null;
  settlement?: V4SettlementOutcome | null;
  settlement_policy?: string;
}

export interface V4ExpectedMove {
  spot: string | null;
  observed_at: string | null;
  implied_move_available: boolean;
  implied_move_dollars: string | null;
  implied_move_pct: string | null;
  upper_implied_boundary: string | null;
  lower_implied_boundary: string | null;
  implied_move_source: string | null;
  historical_sample_n: number | null;
  historical_evidence_quality: string | null;
  historical_median_abs_move_pct: string | null;
  historical_median_upper_boundary: string | null;
  historical_median_lower_boundary: string | null;
  context_version: string | null;
}

export interface V4ScenarioCell {
  scenario_id: string;
  move_label: string;
  em_fraction: string;
  scenario_underlying_price: string;
  iv_label: string;
  iv_multiplier: string;
  return_executable: string | null;
  return_theoretical: string | null;
  reason_codes: string[];
}

export interface V4ScenarioGrid {
  core: V4ScenarioCell[];
  stress: V4ScenarioCell[];
}

export interface V4SettlementQuality {
  total: number;
  counts: {
    EXECUTABLE_BID_ASK: number;
    MARKET_CLOSE_FALLBACK: number;
    EXPIRATION_INTRINSIC_AT_CLOSE: number;
    UNRESOLVED: number;
  };
  executable_settlement_rate: number;
  eod_fallback_rate: number;
  expiration_intrinsic_rate: number;
  unresolved_rate: number;
}

export interface V4ConfigTrackRecordRow {
  configuration_key: string;
  events: number;
  actionable: number;
  no_action: number;
  failed: number;
  entry_observed: number;
  entry_failed: number;
  settled: number;
  settlement_failed: number;
  wins: number | null;
  losses: number | null;
  win_rate: number | null;
  average_standardized_return: number | null;
  median_standardized_return: number | null;
  average_realized_pnl: number | null;
  average_capital_used: number | null;
  sample_sufficiency: string;
  settlement_quality: V4SettlementQuality;
  scored_settlements: number;
}

export type V4TrackRecordView = "all" | "executable_only";

export interface V4TrackRecordByConfiguration {
  notice: string;
  sample_floor: number;
  view: V4TrackRecordView;
  view_note: string;
  settlement_quality: V4SettlementQuality;
  metrics_note: string;
  configurations: V4ConfigTrackRecordRow[];
}

