// Mirrors backend/src/schemas/api.py — kept manually in sync (no codegen
// pipeline yet; see docs/limitations.md).

export interface Company {
  ticker: string;
  name: string;
  sector: string | null;
  exchange: string | null;
}

export interface EarningsResult {
  actual_eps: string | null;
  actual_revenue: string | null;
  gross_margin: string | null;
  reported_at: string | null;
  source_provider: string;
}

export interface PriceReaction {
  close_price_before: string | null;
  next_day_close: string | null;
  five_day_close: string | null;
  next_day_move_pct: string | null;
  five_day_move_pct: string | null;
}

export interface EarningsEventSummary {
  id: number;
  fiscal_year: number;
  fiscal_quarter: number;
  earnings_date: string | null;
  date_confirmed: boolean;
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

export interface ImpliedVsRealizedMove {
  target_earnings_date: string;
  snapshot_timestamp: string;
  near_term_expiration: string | null;
  implied_move_pct: string | null;
  realized_next_day_move_pct: string;
}

export interface CompanyReplaySummary {
  company: Company;
  historical_moves: HistoricalMoveStats | null;
  implied_vs_realized: ImpliedVsRealizedMove[];
}

export interface ReplaySummary {
  companies: CompanyReplaySummary[];
  options_data_ingested: boolean;
}

// Deliberately no market_expectations/implied_move here -- a past earnings
// event's own page never carries forward-looking data (Phase 14 fix for
// mixing historical-event and upcoming-earnings temporal contexts). That
// company-level, always-current data lives on ResearchOverview instead.
export interface EarningsEventDetail extends EarningsEventSummary {
  company: Company;
  result: EarningsResult | null;
  price_reaction: PriceReaction | null;
  historical_moves: HistoricalMoveStats | null;
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

export interface ResearchQueryResponse {
  question: string;
  answer: string;
  citations: Citation[];
  trace: ExecutionTrace;
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

export interface OptionLegInput {
  option_type: "call" | "put";
  action: "buy" | "sell";
  strike: string;
  premium: string;
  quantity?: number;
}

export interface StrategyPayoffRequest {
  strategy_label: string;
  legs: OptionLegInput[];
}

export interface StrategyPayoffResponse {
  summary: string;
  net_premium: string;
  max_profit: string;
  max_loss: string;
  breakevens: string[];
}

export interface ImpliedMoveRequest {
  underlying_price: string;
  strike: string;
  call_price: string;
  put_price: string;
  expiration_label?: string;
}

export interface ImpliedMoveResponse {
  summary: string;
  method: string;
  implied_move_pct: string;
  implied_move_absolute: string;
  expiration_label: string;
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

export interface ProviderCapabilities {
  prices: boolean;
  earnings_estimates: boolean;
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

export interface OptionQuote {
  expiration_date: string;
  strike: string;
  option_type: "call" | "put";
  bid: string | null;
  ask: string | null;
  last_price: string | null;
  volume: number | null;
  open_interest: number | null;
  implied_volatility: string | null;
  delta: string | null;
  gamma: string | null;
  theta: string | null;
  vega: string | null;
  market_data_quality: string | null;
  source_provider: string;
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

export interface ScenarioPnl {
  down_price: string;
  down_pnl: string;
  flat_pnl: string;
  up_price: string;
  up_pnl: string;
}

export interface RankedStrategy {
  rank: number;
  category: string;
  legs: OptionLeg[];
  analysis: StrategyAnalysis;
  score: string;
  explanation: string;
  scenario: ScenarioPnl | null;
  move_compatibility: MoveCompatibility | null;
}

export interface StrategyLab {
  ticker: string;
  expiration: string | null;
  underlying_price: string | null;
  implied_move_pct: string | null;
  strategies: RankedStrategy[];
  chain: OptionQuote[];
  anchor: string | null;
  reason: string | null;
  market_session: string;
  data_state: string;
  snapshot_source: string | null;
  snapshot_timestamp: string | null;
  snapshot_age_minutes: number | null;
  snapshot_age_label: string | null;
  earnings_anchor_status: string;
  options_market: OptionsMarketState;
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

export interface PortfolioPosition {
  account_id_masked: string;
  conid: number;
  contract_description: string;
  asset_class: string;
  quantity: string;
  currency: string | null;
  market_price: string | null;
  market_value: string | null;
  average_cost: string | null;
  unrealized_pnl: string | null;
  realized_pnl: string | null;
  option_expiry: string | null;
  option_right: string | null;
  option_strike: string | null;
  snapshot_timestamp: string;
  source_provider: string;
}

export interface PortfolioSnapshotResponse {
  positions: PortfolioPosition[];
  snapshot_timestamp: string | null;
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

export interface ExpirationScore {
  event_fit: number;
  liquidity: number;
  quote_coverage: number;
  bid_ask_quality: number;
  dte_suitability: number;
  data_quality: number;
  total: number;
}

export interface ExpirationCandidate {
  expiration: string;
  dte: number;
  days_after_earnings: number | null;
  contract_count: number;
  priceable_contract_count: number;
  quote_coverage: string;
  bid_ask_coverage: string;
  oi_coverage: string;
  volume_coverage: string;
  atm_iv: string | null;
  atm_spread_pct: string | null;
  quality: string;
  score: ExpirationScore;
  is_earnings_anchored: boolean;
  excluded_pre_earnings: boolean;
}

export interface ExpirationSelectionResult {
  mode: "auto" | "manual";
  selected: ExpirationCandidate | null;
  alternatives: ExpirationCandidate[];
  reasons: string[];
  warning: string | null;
}

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

export interface SettlementAttemptResult {
  decision: AIDecisionVersion;
  settled: boolean;
  message: string;
}

export interface PendingDecision {
  ticker: string;
  decision: AIDecisionVersion;
}

export interface PendingDecisions {
  pending: PendingDecision[];
  final_count: number;
  pending_count: number;
  settled_count: number;
}

export interface Rate {
  correct: number;
  total: number;
  pct: string | null;
}

export interface ConfidenceBucket {
  label: string;
  lower: number;
  upper: number;
  rate: Rate;
}

export interface TrackRecord {
  window: "all_time" | "last_10";
  evaluated_count: number;
  directional_accuracy: Rate;
  bullish_accuracy: Rate;
  bearish_accuracy: Rate;
  average_confidence: string | null;
  high_confidence_accuracy: Rate;
  volatility_view_accuracy: Rate;
  breakeven_success: Rate;
  strategy_win_rate_available: boolean;
  confidence_calibration: ConfidenceBucket[];
}

// Phase 4.6 -- AI Earnings Analyst Track Record, over the real, settled
// Benchmark Portfolio forward-test decisions (DecisionSnapshot +
// SettlementCaptureAttempt). Deliberately a separate shape from
// TrackRecord above -- that one grades the legacy AI Options Decision
// journal; this one grades whether a real $2,000 benchmark following the
// AI would actually have made money. See docs/PHASE4_6_TRACK_RECORD_
// ARCHITECTURE_REVIEW.md.
export interface BenchmarkTrackRecord {
  portfolio_id: number;
  total_decisions: number;
  settled_decisions: number;
  win_rate: Rate;
  average_r: string | null;
  median_r: string | null;
  expectancy: string | null;
  profit_factor: string | null;
  max_drawdown: string | null;
  max_drawdown_pct: string | null;
  directional_accuracy: Rate;
  breakeven_accuracy: Rate;
  range_accuracy: Rate;
}

export interface BenchmarkCalibrationBucket {
  label: string;
  lower: number | null;
  upper: number | null;
  rate: Rate;
}

export interface BenchmarkCalibration {
  portfolio_id: number;
  settled_decisions: number;
  buckets: BenchmarkCalibrationBucket[];
}

// --- AI Earnings Analyst Dashboard (product frontend layer) --------------
// Reads the same immutable Phase 4 tables the Track Record analytics
// above already read (DecisionSnapshot/EntrySnapshot/EntryCaptureAttempt/
// SettlementCaptureAttempt/ExitSnapshot), never the legacy AIDecisionVersion
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
  confidence_interval: { lower: string; upper: string } | null;
  historical_sample_size: number | null;
  historical_compatibility: Record<string, unknown> | null;

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

  legs: ExitSnapshot[];
}

/** Derived, client-side only -- never returned by any endpoint as a
 * literal field. Mirrors backend services/decision_lifecycle.py's own
 * three stages exactly, computed here from the same real facts (a
 * CAPTURED EntryCaptureAttempt / SettlementCaptureAttempt existing for
 * the decision) rather than duplicating any calculation. */
export type DecisionLifecycleStage = "pending_entry" | "entered" | "settled";
