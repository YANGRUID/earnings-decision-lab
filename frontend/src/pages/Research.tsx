import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { ResearchQueryResponse } from "../types/api";

const EXAMPLE_QUESTIONS = [
  "What were MU's last two earnings results?",
  "What did MU say about HBM demand in its risk factors?",
  "How has AMD's guidance changed recently?",
];

export function Research() {
  const [question, setQuestion] = useState("");
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
        <h1>AI Research</h1>
        <p>
          Grounded, cited answers over real earnings data and SEC filings — every answer shows
          which tools were called and how it was verified, not just the final text.
        </p>
      </div>

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
