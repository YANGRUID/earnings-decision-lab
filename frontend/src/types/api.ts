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

export interface SystemStatus {
  counts: DataCounts;
  freshness: DataFreshness;
  llm: LlmConfigStatus;
  embedding_model: string;
  evaluation: EvaluationStatusResponse;
}

export interface PreparationStep {
  step: string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  detail: string | null;
  updated_at: string;
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
