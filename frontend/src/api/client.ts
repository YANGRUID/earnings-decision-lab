import type {
  Company,
  EarningsEstimate,
  EarningsEventDetail,
  EarningsEventSummary,
  EarningsThesis,
  EvaluationStatusResponse,
  FilingSearchResponse,
  ImpliedMoveRequest,
  ImpliedMoveResponse,
  PortfolioSnapshotResponse,
  ReplaySummary,
  ResearchJob,
  ResearchJobQueued,
  ResearchOverview,
  ResearchQueryResponse,
  StrategyLab,
  StrategyPayoffRequest,
  StrategyPayoffResponse,
  SystemStatus,
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

  researchQuery: (question: string) =>
    request<ResearchQueryResponse>("/research/query", {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
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
  getStrategyLab: (ticker: string) => request<StrategyLab>(`/research/${ticker}/strategies`),
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

  getPortfolioPositions: (params: { ticker?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.ticker) qs.set("ticker", params.ticker);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<PortfolioSnapshotResponse>(`/portfolio/positions${suffix}`);
  },
};
