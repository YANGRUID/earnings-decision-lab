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
  near_term_expiration: string | null;
  atm_iv_near: string | null;
  implied_move_pct: string | null;
  implied_move_absolute: string | null;
  inputs: Record<string, unknown> | null;
  snapshot_timestamp: string;
  computed_at: string;
}

export interface EarningsEventDetail extends EarningsEventSummary {
  company: Company;
  result: EarningsResult | null;
  price_reaction: PriceReaction | null;
  market_expectations: EarningsEstimate | null;
  implied_move: VolatilitySnapshot | null;
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

export interface ApiError {
  error: string;
  request_id: string | null;
}
