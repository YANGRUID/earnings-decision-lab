import { useState } from "react";
import { useAsync } from "../../hooks/useAsync";
import { api } from "../../api/client";
import { LoadingState, ErrorState } from "../../components/StatusStates";
import { providerLabel, DOMAIN_LABELS } from "../../lib/format";
import type { ProviderUsageSummary } from "../../types/api";

const WINDOWS: { value: string; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "all_time", label: "All time" },
];

function isLlm(domain: string): boolean {
  return domain === "llm";
}

function ProviderUsageRow({ row }: { row: ProviderUsageSummary }) {
  return (
    <tr>
      <td>{providerLabel(row.provider)}</td>
      <td className="text-sm text-muted">{DOMAIN_LABELS[row.domain] ?? row.domain}</td>
      <td className="mono">{row.request_count}</td>
      <td className="mono">{row.error_count}</td>
      <td className="mono">{row.rate_limited_count}</td>
      <td className="mono">
        {row.avg_latency_ms !== null ? `${Math.round(row.avg_latency_ms)} ms` : "—"}
      </td>
      <td className="mono">
        {isLlm(row.domain)
          ? row.total_tokens !== null
            ? row.total_tokens.toLocaleString()
            : "unavailable"
          : "—"}
      </td>
      <td className="mono">
        {row.estimated_cost !== null ? `$${Number(row.estimated_cost).toFixed(4)}` : "Cost unavailable"}
      </td>
      <td className="text-sm text-muted">
        {row.last_event_at ? new Date(row.last_event_at).toLocaleString() : "never"}
      </td>
    </tr>
  );
}

export function ApiUsage() {
  const [window, setWindow] = useState("7d");
  const usage = useAsync(() => api.getUsageSummary(window), [window]);

  return (
    <div>
      <div className="page-header">
        <h1>API Usage</h1>
        <p>
          Real, recorded calls through every provider adapter — LLM token counts only when a
          provider's own response actually reports them, data-API request counts always. Nothing
          here is estimated: a metric this project can't honestly compute (e.g. cost, when a
          provider doesn't report it) shows as unavailable rather than a guess.
        </p>
      </div>

      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        {WINDOWS.map((w) => (
          <button
            key={w.value}
            className="btn-secondary"
            style={{
              borderColor: window === w.value ? "var(--color-accent)" : undefined,
              fontWeight: window === w.value ? 600 : undefined,
            }}
            onClick={() => setWindow(w.value)}
          >
            {w.label}
          </button>
        ))}
      </div>

      {usage.loading && !usage.data && <LoadingState label="Loading usage…" />}
      {usage.error && !usage.data && <ErrorState message={usage.error} />}
      {usage.data && (
        <>
          <div className="grid grid-4" style={{ gap: 10, marginBottom: 20 }}>
            <div className="stat card">
              <span className="stat-label">Total requests</span>
              <span className="stat-value">{usage.data.total_requests}</span>
            </div>
            <div className="stat card">
              <span className="stat-label">Errors</span>
              <span className="stat-value">{usage.data.total_errors}</span>
            </div>
            <div className="stat card">
              <span className="stat-label">Rate-limit events</span>
              <span className="stat-value">{usage.data.total_rate_limited}</span>
            </div>
            <div className="stat card">
              <span className="stat-label">LLM tokens</span>
              <span className="stat-value">
                {usage.data.total_llm_tokens !== null
                  ? usage.data.total_llm_tokens.toLocaleString()
                  : "unavailable"}
              </span>
            </div>
          </div>

          <div className="card">
            <h2>Per-provider breakdown</h2>
            {usage.data.providers.length === 0 ? (
              <p className="text-sm text-muted">No provider calls recorded in this window yet.</p>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table>
                  <thead>
                    <tr>
                      <th>Provider</th>
                      <th>Domain</th>
                      <th>Requests</th>
                      <th>Errors</th>
                      <th>Rate limited</th>
                      <th>Avg latency</th>
                      <th>Tokens</th>
                      <th>Estimated cost</th>
                      <th>Last call</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.data.providers.map((row) => (
                      <ProviderUsageRow key={`${row.provider}-${row.domain}`} row={row} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="text-sm text-muted" style={{ marginTop: 12, marginBottom: 0 }}>
              Estimated cost is only ever shown when derived from a provider-reported cost or an
              owner-configured price — this project does not maintain a hard-coded pricing table
              that could silently go stale, so "Cost unavailable" is the honest default today.
            </p>
          </div>
        </>
      )}
    </div>
  );
}
