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
  budget_fit: BudgetFit | null;
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
  recommended_strategy_category: string | null;
  recommended_strategy_legs: OptionLeg[] | null;
  recommended_strategy_analysis: StrategyAnalysis | null;
  recommended_strategy_score: number | null;
  recommended_strategy_score_components: Record<string, number> | null;
  recommended_strategy_why: string[] | null;
  recommended_strategy_risks: string[] | null;
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
