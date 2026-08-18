import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { dataStateLabel, formatRelativeTime } from "../lib/format";
import type { ResearchOverview, ResearchQueryResponse } from "../types/api";

const DEFAULT_EXAMPLE_QUESTIONS = [
  "What were MU's last two earnings results?",
  "What did MU say about HBM demand in its risk factors?",
  "How has AMD's guidance changed recently?",
];

interface ChecklistItem {
  label: string;
  ok: boolean;
  detail: string;
}

function buildChecklist(overview: ResearchOverview): ChecklistItem[] {
  return [
    {
      label: "Historical earnings",
      ok: overview.earnings_events_count > 0,
      detail:
        overview.earnings_events_count > 0
          ? `${overview.earnings_events_count} reported events on record`
          : "None on record yet",
    },
    {
      label: "SEC filings",
      ok: overview.filings_count > 0,
      detail:
        overview.filings_count > 0
          ? `${overview.filings_count} filings, ${overview.filing_chunks_count} searchable excerpts`
          : "None ingested yet",
    },
    {
      label: "Price history",
      ok: overview.price_bars_count > 0,
      detail:
        overview.price_bars_count > 0
          ? `${overview.price_bars_count} daily price bars`
          : "No price history yet",
    },
    {
      label: "Analyst consensus",
      ok: overview.latest_earnings_estimate !== null,
      detail: overview.latest_earnings_estimate
        ? `From ${overview.latest_earnings_estimate.source_provider}`
        : "No consensus collected yet",
    },
    {
      label: "Options snapshot",
      ok: overview.options_snapshot_source !== null,
      detail: overview.options_snapshot_source
        ? `${dataStateLabel(overview.options_data_state)} · ${overview.options_snapshot_age_label ?? ""} old`
        : "Not collected yet",
    },
  ];
}

function ResearchChecklist({ ticker }: { ticker: string }) {
  const overview = useAsync(() => api.getResearchOverview(ticker), [ticker]);
  if (overview.loading && !overview.data) return null;
  if (!overview.data || !overview.data.company) return null;

  const items = buildChecklist(overview.data);
  return (
    <div className="card">
      <h2>What's on record for {ticker}</h2>
      <ul className="freshness-list" style={{ marginBottom: 8 }}>
        {items.map((item) => (
          <li key={item.label}>
            <span style={{ color: item.ok ? "var(--color-positive)" : "var(--color-text-faint)" }}>
              {item.ok ? "✓" : "⚠"}
            </span>{" "}
            <strong>{item.label}</strong> — {item.detail}
          </li>
        ))}
      </ul>
      <p className="text-sm text-faint" style={{ margin: 0 }}>
        Evidence last refreshed{" "}
        {overview.data.latest_job?.completed_at
          ? formatRelativeTime(overview.data.latest_job.completed_at)
          : "never — this company hasn't been prepared yet"}
        . Answers below are only ever grounded in what's checked off here.
      </p>
    </div>
  );
}

export function Research() {
  const [searchParams] = useSearchParams();
  const contextTicker = searchParams.get("ticker")?.toUpperCase() ?? null;
  const EXAMPLE_QUESTIONS = contextTicker
    ? [
        `What were ${contextTicker}'s last two earnings results?`,
        `What did ${contextTicker} say about risk factors in its most recent filing?`,
        `How has ${contextTicker}'s guidance changed recently?`,
      ]
    : DEFAULT_EXAMPLE_QUESTIONS;
  const [question, setQuestion] = useState(contextTicker ? `About ${contextTicker}: ` : "");
  const [response, setResponse] = useState<ResearchQueryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const ask = async (q: string) => {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    setResponse(null);
    try {
      const res = await api.researchQuery(q);
      setResponse(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The research query failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div className="page-header">
        <h1>AI Research{contextTicker ? ` — ${contextTicker}` : ""}</h1>
        <p>
          Grounded, cited answers over real earnings data and SEC filings — every answer shows
          which tools were called and how it was verified, not just the final text.
        </p>
      </div>

      {contextTicker && <ResearchChecklist ticker={contextTicker} />}

      <div className="card">
        <div className="field">
          <label>Question</label>
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            rows={2}
            style={{ width: "100%", resize: "vertical" }}
            placeholder="Ask about a covered company's earnings, filings, or guidance…"
          />
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button className="btn" onClick={() => ask(question)} disabled={loading}>
            {loading ? "Researching…" : "Ask"}
          </button>
          <span className="text-sm text-faint">or try:</span>
          {EXAMPLE_QUESTIONS.map((q) => (
            <button
              key={q}
              className="btn-secondary"
              style={{ fontSize: 12, padding: "5px 10px" }}
              onClick={() => {
                setQuestion(q);
                ask(q);
              }}
            >
              {q}
            </button>
          ))}
        </div>
      </div>

      {error && <div className="notice">{error}</div>}

      {response && (
        <>
          <div className="card">
            <h2>Answer</h2>
            <p style={{ whiteSpace: "pre-wrap", marginTop: 0 }}>{response.answer}</p>
          </div>

          {response.citations.length > 0 && (
            <div className="card">
              <h2>Citations</h2>
              <ul className="citation-list">
                {response.citations.map((c) => (
                  <li key={c.marker} className="citation-item">
                    <span className="citation-marker">{c.marker}</span>
                    {c.ticker} {c.filing_type} filed {c.filing_date}
                    {c.section ? `, ${c.section}` : ""} —{" "}
                    <a href={c.source_url} target="_blank" rel="noreferrer">
                      source
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="card">
            <h2>Execution trace</h2>
            <div className="grid grid-3" style={{ marginBottom: 14 }}>
              <div className="stat">
                <span className="stat-label">Intent</span>
                <span className="stat-value small">{response.trace.intent_category}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Planning</span>
                <span className="stat-value small">{response.trace.planning_method}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Model</span>
                <span className="stat-value small">{response.trace.model}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Duration</span>
                <span className="stat-value small">
                  {(response.trace.total_duration_ms / 1000).toFixed(1)}s
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">Tokens (in/out)</span>
                <span className="stat-value small">
                  {response.trace.total_input_tokens} / {response.trace.total_output_tokens}
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">Est. cost</span>
                <span className="stat-value small">
                  {response.trace.estimated_cost_usd
                    ? `$${Number(response.trace.estimated_cost_usd).toFixed(4)}`
                    : "n/a"}
                </span>
              </div>
            </div>

            {response.trace.tool_calls.length === 0 ? (
              <p className="text-sm text-muted">No tools were needed for this question.</p>
            ) : (
              response.trace.tool_calls.map((tc, i) => (
                <div className="trace-step" key={i}>
                  <div className="trace-step-header">
                    <span className="trace-step-name">{tc.tool_name}</span>
                    <span
                      className={`pill ${tc.success ? "pill-positive" : "pill-negative"}`}
                    >
                      {tc.success ? "ok" : "failed"} · {tc.duration_ms.toFixed(0)}ms
                    </span>
                  </div>
                  <div className="text-muted" style={{ marginTop: 4 }}>
                    {tc.summary || tc.error}
                  </div>
                  {tc.query_description && (
                    <details style={{ marginTop: 6 }}>
                      <summary className="text-sm text-faint" style={{ cursor: "pointer" }}>
                        Query
                      </summary>
                      <pre
                        className="mono text-sm"
                        style={{ whiteSpace: "pre-wrap", marginTop: 4 }}
                      >
                        {tc.query_description}
                      </pre>
                    </details>
                  )}
                </div>
              ))
            )}

            <div style={{ marginTop: 10 }}>
              <span className="stat-label">Verification</span>{" "}
              {response.trace.verification_ran ? (
                <span
                  className={`pill ${response.trace.verification_supported ? "pill-positive" : "pill-negative"}`}
                >
                  {response.trace.verification_supported ? "supported by evidence" : "revised"}
                </span>
              ) : (
                <span className="pill pill-neutral">not run (no evidence to check)</span>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
