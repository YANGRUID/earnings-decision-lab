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

export interface EarningsEventDetail extends EarningsEventSummary {
  company: Company;
  result: EarningsResult | null;
  price_reaction: PriceReaction | null;
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

export interface ApiError {
  error: string;
  request_id: string | null;
}
