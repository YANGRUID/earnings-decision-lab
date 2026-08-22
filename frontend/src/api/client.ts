import type {
  AIDecisionVersion,
  AIResearchHistoryItem,
  AIThesisVersion,
  BenchmarkCalibration,
  BenchmarkTrackRecord,
  Company,
  DecisionDirection,
  DecisionSnapshot,
  DecisionVolatilityView,
  EarningsCalendarEvent,
  EarningsEstimate,
  EarningsEventDetail,
  EarningsEventSummary,
  EarningsThesis,
  EntryCaptureAttempt,
  EvaluationStatusResponse,
  ExpirationSelectionResult,
  FilingSearchResponse,
  IbkrConnectResponse,
  ImpliedMoveRequest,
  ImpliedMoveResponse,
  PendingDecisions,
  PortfolioSnapshotResponse,
  ProviderDashboard,
  ProviderSettingsUpdate,
  ReplaySummary,
  ResearchJob,
  ResearchJobQueued,
  ResearchOverview,
  ResearchQueryResponse,
  RiskProfile,
  SettlementAttemptResult,
  SettlementCaptureAttempt,
  StrategyLab,
  StrategyPayoffRequest,
  StrategyPayoffResponse,
  SystemStatus,
  TestConnectionResult,
  TrackRecord,
  UsageSummary,
} from "../types/api";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";

export class ApiError extends Error {
  status: number;
  requestId: string | null;

  constructor(status: number, message: string, requestId: string | null) {
    super(message);
    this.status = status;
    this.requestId = requestId;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let message = response.statusText;
    let requestId: string | null = null;
    try {
      const body = await response.json();
      message = body.error ?? message;
      requestId = body.request_id ?? null;
    } catch {
      // non-JSON error body — fall back to statusText
    }
    throw new ApiError(response.status, message, requestId);
  }
  return response.json() as Promise<T>;
}

export const api = {
  listCompanies: () => request<Company[]>("/companies"),
  getCompany: (ticker: string) => request<Company>(`/companies/${ticker}`),

  listEarnings: (params: { ticker?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.ticker) qs.set("ticker", params.ticker);
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<EarningsEventSummary[]>(`/earnings${suffix}`);
  },
  getEarningsEvent: (id: number) => request<EarningsEventDetail>(`/earnings/${id}`),

  calculatePayoff: (body: StrategyPayoffRequest) =>
    request<StrategyPayoffResponse>("/options/strategies/payoff", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  calculateImpliedMove: (body: ImpliedMoveRequest) =>
    request<ImpliedMoveResponse>("/options/implied-move", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  researchQuery: (question: string, ticker?: string) =>
    request<ResearchQueryResponse>("/research/query", {
      method: "POST",
      body: JSON.stringify({ question, ticker: ticker ?? null }),
    }),
  getResearchHistory: (params: { ticker?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.ticker) qs.set("ticker", params.ticker);
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<AIResearchHistoryItem[]>(`/research/history${suffix}`);
  },
  getResearchHistoryItem: (id: number) =>
    request<AIResearchHistoryItem>(`/research/history/${id}`),
  deleteResearchHistoryItem: (id: number) =>
    request<void>(`/research/history/${id}`, { method: "DELETE" }),

  getThesisHistory: (ticker: string, params: { limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<AIThesisVersion[]>(`/research/${ticker}/theses${suffix}`);
  },
  getThesisVersion: (ticker: string, id: number) =>
    request<AIThesisVersion>(`/research/${ticker}/theses/${id}`),
  deleteThesisVersion: (ticker: string, id: number) =>
    request<void>(`/research/${ticker}/theses/${id}`, { method: "DELETE" }),
  searchDocuments: (params: { query: string; ticker?: string; k?: number }) => {
    const qs = new URLSearchParams({ query: params.query });
    if (params.ticker) qs.set("ticker", params.ticker);
    if (params.k) qs.set("k", String(params.k));
    return request<FilingSearchResponse>(`/research/documents?${qs.toString()}`);
  },

  prepareResearch: (ticker: string) =>
    request<ResearchJob | ResearchJobQueued>(`/research/${ticker}/prepare`, { method: "POST" }),
  refreshResearch: (ticker: string) =>
    request<ResearchJob | ResearchJobQueued>(`/research/${ticker}/refresh`, { method: "POST" }),
  getResearchStatus: (ticker: string) => request<ResearchJob>(`/research/${ticker}/status`),
  getResearchOverview: (ticker: string) =>
    request<ResearchOverview>(`/research/${ticker}/overview`),
  getStrategyLab: (ticker: string, expiration?: string) =>
    request<StrategyLab>(
      `/research/${ticker}/strategies${expiration ? `?expiration=${encodeURIComponent(expiration)}` : ""}`
    ),
  getEarningsThesis: (ticker: string) =>
    request<EarningsThesis>(`/research/${ticker}/thesis`, { method: "POST" }),
  setEarningsDateOverride: (
    ticker: string,
    body: { estimated_report_date: string; fiscal_period_end_date?: string | null }
  ) =>
    request<EarningsEstimate>(`/research/${ticker}/earnings-date`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getLatestEvaluation: () => request<EvaluationStatusResponse>("/evaluations/latest"),

  getReplaySummary: () => request<ReplaySummary>("/replay"),

  getSystemStatus: () => request<SystemStatus>("/system-status"),
  getIbkrConnectUrl: () => request<IbkrConnectResponse>("/ibkr/connect"),

  getProviderDashboard: () => request<ProviderDashboard>("/settings/providers"),
  updateProviderSettings: (update: ProviderSettingsUpdate) =>
    request<ProviderDashboard>("/settings/providers", {
      method: "PUT",
      body: JSON.stringify(update),
    }),
  testProviderConnection: (domain: string, provider: string) =>
    request<TestConnectionResult>(`/settings/providers/${domain}/${provider}/test`, {
      method: "POST",
    }),
  setProviderCredential: (
    provider: string,
    body: { api_key: string; base_url?: string | null; model?: string | null }
  ) =>
    request<ProviderDashboard>(`/settings/providers/${provider}/credential`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteProviderCredential: (provider: string) =>
    request<ProviderDashboard>(`/settings/providers/${provider}/credential`, {
      method: "DELETE",
    }),
  getUsageSummary: (window: string) =>
    request<UsageSummary>(`/settings/usage?window=${encodeURIComponent(window)}`),

  getPortfolioPositions: (params: { ticker?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.ticker) qs.set("ticker", params.ticker);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<PortfolioSnapshotResponse>(`/portfolio/positions${suffix}`);
  },

  generateDecision: (
    ticker: string,
    options?: {
      direction?: DecisionDirection;
      volatility_view?: DecisionVolatilityView;
      trade_budget?: string;
      risk_cap?: string;
      risk_cap_is_percent?: boolean;
      risk_profile?: RiskProfile;
      expiration?: string;
    }
  ) =>
    request<AIDecisionVersion>(`/research/${ticker}/decision`, {
      method: "POST",
      body: options ? JSON.stringify(options) : undefined,
    }),
  getExpirationSelection: (
    ticker: string,
    params: { mode?: "auto" | "manual"; expiration?: string; max_candidates?: number } = {}
  ) => {
    const qs = new URLSearchParams();
    if (params.mode) qs.set("mode", params.mode);
    if (params.expiration) qs.set("expiration", params.expiration);
    if (params.max_candidates) qs.set("max_candidates", String(params.max_candidates));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<ExpirationSelectionResult>(`/research/${ticker}/expirations${suffix}`);
  },
  getDecisionHistory: (ticker: string, params: { limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<AIDecisionVersion[]>(`/research/${ticker}/decisions${suffix}`);
  },
  getDecision: (ticker: string, id: number) =>
    request<AIDecisionVersion>(`/research/${ticker}/decisions/${id}`),
  deleteDecision: (ticker: string, id: number) =>
    request<void>(`/research/${ticker}/decisions/${id}`, { method: "DELETE" }),
  markDecisionFinal: (ticker: string, id: number) =>
    request<AIDecisionVersion>(`/research/${ticker}/decisions/${id}/final`, { method: "POST" }),
  settleDecision: (ticker: string, id: number) =>
    request<SettlementAttemptResult>(`/research/${ticker}/decisions/${id}/settle`, {
      method: "POST",
    }),
  getPendingDecisions: () => request<PendingDecisions>("/research/decisions/pending"),

  getTrackRecord: (params: { ticker?: string; window?: "all_time" | "last_10" } = {}) => {
    const qs = new URLSearchParams();
    if (params.ticker) qs.set("ticker", params.ticker);
    if (params.window) qs.set("window", params.window);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<TrackRecord>(`/research/track-record${suffix}`);
  },

  // Phase 4.6 -- AI Earnings Analyst Track Record, over the Benchmark
  // Portfolio's real, settled forward-test decisions. Distinct from
  // getTrackRecord above (a different system, over the legacy AI
  // Options Decision journal).
  getBenchmarkTrackRecord: (
    params: {
      portfolioId?: number;
      strategy?: string;
      confidenceBucket?: string;
      dteBucket?: string;
      riskProfile?: RiskProfile;
      ivRegime?: string;
    } = {}
  ) => {
    const qs = new URLSearchParams();
    if (params.portfolioId) qs.set("portfolio_id", String(params.portfolioId));
    if (params.strategy) qs.set("strategy", params.strategy);
    if (params.confidenceBucket) qs.set("confidence_bucket", params.confidenceBucket);
    if (params.dteBucket) qs.set("dte_bucket", params.dteBucket);
    if (params.riskProfile) qs.set("risk_profile", params.riskProfile);
    if (params.ivRegime) qs.set("iv_regime", params.ivRegime);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<BenchmarkTrackRecord>(`/benchmark/track-record${suffix}`);
  },
  getBenchmarkCalibration: (params: { portfolioId?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.portfolioId) qs.set("portfolio_id", String(params.portfolioId));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<BenchmarkCalibration>(`/benchmark/calibration${suffix}`);
  },

  // AI Earnings Analyst Dashboard -- reads the same immutable Phase 4
  // tables the benchmark track record above already reads.
  listUpcomingEarnings: (params: { limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<EarningsCalendarEvent[]>(`/earnings-calendar${suffix}`);
  },
  getSymbolEarningsCalendar: (symbol: string) =>
    request<EarningsCalendarEvent[]>(`/earnings-calendar/${symbol}`),
  listEarningsByMonth: (year: number, month: number) =>
    request<EarningsCalendarEvent[]>(`/earnings-calendar/by-month?year=${year}&month=${month}`),

  listDecisionSnapshots: (params: { ticker?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.ticker) qs.set("ticker", params.ticker);
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<DecisionSnapshot[]>(`/decision-snapshots${suffix}`);
  },
  getDecisionSnapshot: (id: number) => request<DecisionSnapshot>(`/decision-snapshots/${id}`),
  getDecisionSnapshotEntries: (id: number) =>
    request<EntryCaptureAttempt[]>(`/decision-snapshots/${id}/entries`),

  listBenchmarkEntries: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<EntryCaptureAttempt[]>(`/benchmark/entries${suffix}`);
  },

  getSettlements: (decisionId: number) =>
    request<SettlementCaptureAttempt[]>(`/settlements/${decisionId}`),
  listAllSettlements: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<SettlementCaptureAttempt[]>(`/settlements${suffix}`);
  },
};
