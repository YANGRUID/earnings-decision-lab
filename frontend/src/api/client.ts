import type {
  AIResearchHistoryItem,
  AIThesisVersion,
  Company,
  EarningsCalendarEvent,
  EarningsEstimate,
  EarningsThesis,
  IbkrConnectResponse,
  OperationsEvents,
  OperationsFailures,
  OperationsJobs,
  OperationsSummary,
  PreparationProgress,
  ProviderDashboard,
  ProviderSettingsUpdate,
  ResearchJob,
  ResearchJobQueued,
  ResearchOverview,
  ResearchOverviewListResponse,
  ResearchQueryResponse,
  SystemStatus,
  TestConnectionResult,
  UsageSummary,
  V4ShadowCandidatesResponse,
  V4ShadowDecisionDetail,
  V4ShadowDecisionsResponse,
  V4ShadowTrackRecord,
  V4ShadowConfigurationsResponse,
  V4MethodologyComparison,
  V4TrackRecordByConfiguration,
  V4TrackRecordView,
} from "../types/api";

import { cachedStatus } from "../lib/statusCache";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";
// Status reads are shared for a few seconds; pollers call invalidateStatus().
const STATUS_TTL_MS = 5_000;

export class ApiError extends Error {
  status: number;
  requestId: string | null;

  constructor(status: number, message: string, requestId: string | null) {
    super(message);
    this.status = status;
    this.requestId = requestId;
  }
}

export interface RequestOptions {
  /** Forwarded to fetch(); useAsync passes one so an in-flight request is
   * cancelled when the caller unmounts or its inputs change. */
  signal?: AbortSignal;
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
  prepareResearch: (ticker: string) =>
    request<ResearchJob | ResearchJobQueued>(`/research/${ticker}/prepare`, { method: "POST" }),
  refreshResearch: (ticker: string) =>
    request<ResearchJob | ResearchJobQueued>(`/research/${ticker}/refresh`, { method: "POST" }),
  getResearchStatus: (ticker: string) => request<ResearchJob>(`/research/${ticker}/status`),
  listResearchOverviews: (opts: RequestOptions = {}) =>
    request<ResearchOverviewListResponse>("/research/overviews", { signal: opts.signal }),
  getResearchOverview: (ticker: string) =>
    request<ResearchOverview>(`/research/${ticker}/overview`),
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

  getSystemStatus: () =>
    cachedStatus("system-status", STATUS_TTL_MS, () => request<SystemStatus>("/system-status")),
  getIbkrConnectUrl: () => request<IbkrConnectResponse>("/ibkr/connect"),

  // V4.5 -- EXPERIMENTAL shadow cohort, read-only.
  getV4ShadowDecisions: (params: { ticker?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.ticker) q.set("ticker", params.ticker);
    if (params.limit) q.set("limit", String(params.limit));
    const suffix = q.toString() ? `?${q}` : "";
    return request<V4ShadowDecisionsResponse>(`/v4/shadow/decisions${suffix}`);
  },
  getV4ShadowDecision: (id: number) =>
    request<V4ShadowDecisionDetail>(`/v4/shadow/decisions/${id}`),
  getV4ShadowCandidates: (id: number) =>
    request<V4ShadowCandidatesResponse>(`/v4/shadow/decisions/${id}/candidates`),
  getV4ShadowTrackRecord: () =>
    request<V4ShadowTrackRecord>("/v4/shadow/track-record"),
  // V4 consolidation -- six-configuration read models. All read-only.
  getV4ShadowConfigurations: (id: number) =>
    request<V4ShadowConfigurationsResponse>(`/v4/shadow/decisions/${id}/configurations`),
  // V4.2 challenger research surface (read-only).
  getV4MethodologyComparison: () =>
    request<V4MethodologyComparison>("/v4-2/challenger/comparison"),
  getV4TrackRecordByConfiguration: (view: V4TrackRecordView = "all") =>
    request<V4TrackRecordByConfiguration>(
      `/v4/shadow/track-record/by-configuration?view=${view}`,
    ),
  // Status-style endpoints are read through a short shared cache (see
  // lib/statusCache.ts): several components on one screen ask for the same
  // summary at the same moment, and a navigation must never fire duplicates.
  getOperationsSummary: () =>
    cachedStatus("operations/summary", STATUS_TTL_MS, () => request<OperationsSummary>("/operations/summary")),
  getOperationsEvents: (opts: RequestOptions & { includePast?: boolean } = {}) =>
    request<OperationsEvents>(`/operations/events${opts.includePast ? "?include_past=true" : ""}`, {
      signal: opts.signal,
    }),
  getOperationsJobs: (opts: RequestOptions = {}) =>
    request<OperationsJobs>("/operations/jobs", { signal: opts.signal }),
  getOperationsFailures: (opts: RequestOptions = {}) =>
    request<OperationsFailures>("/operations/failures", { signal: opts.signal }),
  getOperationsPreparationProgress: () =>
    cachedStatus("operations/preparation-progress", STATUS_TTL_MS, () =>
      request<PreparationProgress>("/operations/preparation-progress"),
    ),
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


};
