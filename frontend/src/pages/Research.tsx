import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { Markdown } from "../components/Markdown";
import { dataStateLabel, formatRelativeTime, providerLabel } from "../lib/format";
import type { AIResearchHistoryItem, ResearchOverview, ResearchQueryResponse } from "../types/api";

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
      ok: overview.options_market.chain_exists,
      detail: overview.options_market.chain_exists
        ? `${dataStateLabel(overview.options_market.data_state)} · ${overview.options_market.snapshot_age_label ?? ""} old`
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

function groupByRecency(items: AIResearchHistoryItem[]): { label: string; items: AIResearchHistoryItem[] }[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);

  const groups = new Map<string, AIResearchHistoryItem[]>();
  for (const item of items) {
    const created = new Date(item.created_at);
    const createdDay = new Date(created);
    createdDay.setHours(0, 0, 0, 0);
    let label: string;
    if (createdDay.getTime() === today.getTime()) label = "Today";
    else if (createdDay.getTime() === yesterday.getTime()) label = "Yesterday";
    else label = createdDay.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const list = groups.get(label) ?? [];
    list.push(item);
    groups.set(label, list);
  }
  return [...groups.entries()].map(([label, items]) => ({ label, items }));
}

function HistoryPanel({
  ticker,
  activeId,
  onSelect,
  onDeleted,
}: {
  ticker: string | null;
  activeId: number | null;
  onSelect: (item: AIResearchHistoryItem) => void;
  onDeleted: (id: number) => void;
}) {
  const history = useAsync(
    () => api.getResearchHistory({ ticker: ticker ?? undefined, limit: 15 }),
    [ticker],
  );
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const remove = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation();
    setDeletingId(id);
    try {
      await api.deleteResearchHistoryItem(id);
      onDeleted(id);
      history.reload();
    } finally {
      setDeletingId(null);
    }
  };

  if (history.loading && !history.data) return null;
  if (!history.data || history.data.length === 0) return null;

  const groups = groupByRecency(history.data);

  return (
    <div className="card">
      <h2>Recent research{ticker ? ` — ${ticker}` : ""}</h2>
      {groups.map((group) => (
        <div key={group.label} style={{ marginBottom: 10 }}>
          <div className="text-sm text-faint" style={{ marginBottom: 4 }}>
            {group.label}
          </div>
          <ul className="history-list">
            {group.items.map((item) => (
              <li
                key={item.id}
                className={`history-item ${item.id === activeId ? "active" : ""}`}
                onClick={() => onSelect(item)}
              >
                <span className="history-item-question">
                  {item.ticker && <span className="mono text-faint">{item.ticker} — </span>}
                  {item.question}
                </span>
                <button
                  className="history-item-delete"
                  onClick={(e) => remove(e, item.id)}
                  disabled={deletingId === item.id}
                  aria-label="Delete this research item"
                  title="Delete"
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function AnswerPanel({ item }: { item: AIResearchHistoryItem }) {
  return (
    <>
      <div className="card">
        <h2>Question</h2>
        <p style={{ margin: 0 }}>&ldquo;{item.question}&rdquo;</p>
      </div>

      <div className="card">
        <h2>Answer</h2>
        <Markdown>{item.answer_markdown}</Markdown>
      </div>

      {item.citations.length > 0 && (
        <div className="card">
          <h2>Sources</h2>
          {item.citations.map((c) => (
            <div key={c.marker} className="source-item">
              <span className="citation-badge">{c.marker}</span>
              <div>
                <div className="source-title">
                  {c.ticker} · {c.filing_type} · filed {c.filing_date}
                  {c.section ? ` · ${c.section}` : ""}
                </div>
                <a className="text-link text-sm" href={c.source_url} target="_blank" rel="noreferrer">
                  View source
                </a>
              </div>
            </div>
          ))}
        </div>
      )}

      <details className="card">
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>Research details</summary>
        <div className="grid grid-3" style={{ marginTop: 14, marginBottom: 14 }}>
          <div className="stat">
            <span className="stat-label">Generated</span>
            <span className="stat-value small">{new Date(item.created_at).toLocaleString()}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Intent</span>
            <span className="stat-value small">{item.intent_category}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Planning</span>
            <span className="stat-value small">{item.planning_method}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Provider / model</span>
            <span className="stat-value small">
              {providerLabel(item.provider)} · {item.model}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Duration</span>
            <span className="stat-value small">{(item.total_duration_ms / 1000).toFixed(1)}s</span>
          </div>
          <div className="stat">
            <span className="stat-label">Tokens (in/out)</span>
            <span className="stat-value small">
              {item.total_input_tokens} / {item.total_output_tokens}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Est. cost</span>
            <span className="stat-value small">
              {item.estimated_cost_usd ? `$${Number(item.estimated_cost_usd).toFixed(4)}` : "n/a"}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Verification</span>
            {item.verification_ran ? (
              <span className={`pill ${item.verification_supported ? "pill-positive" : "pill-negative"}`}>
                {item.verification_supported ? "supported by evidence" : "revised"}
              </span>
            ) : (
              <span className="pill pill-neutral">not run</span>
            )}
          </div>
        </div>

        {item.tool_calls.length === 0 ? (
          <p className="text-sm text-muted">No tools were needed for this question.</p>
        ) : (
          item.tool_calls.map((tc, i) => (
            <div className="trace-step" key={i}>
              <div className="trace-step-header">
                <span className="trace-step-name">{tc.tool_name}</span>
                <span className={`pill ${tc.success ? "pill-positive" : "pill-negative"}`}>
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
                  <pre className="mono text-sm" style={{ whiteSpace: "pre-wrap", marginTop: 4 }}>
                    {tc.query_description}
                  </pre>
                </details>
              )}
            </div>
          ))
        )}
      </details>
    </>
  );
}

// Part A11 -- an honest state instead of ever rendering AnswerPanel with
// nothing real behind it. Only "completed" and "insufficient_evidence"
// carry a real, persisted answer (see ask() below); every other status
// gets its own plain, honest notice instead.
// V4 consolidation, Section 42 -- a live "Preparing research for X…"
// state with queue position and current stage, read from the real
// preparation-progress endpoint, instead of a generic not-ready notice.
function PreparingNotice({ tickers }: { tickers: string }) {
  const progress = useAsync(() => api.getOperationsPreparationProgress(), []);
  const p = progress.data;
  const working = p?.current_symbol && tickers.includes(p.current_symbol);
  return (
    <div className="notice" data-testid="research-preparing">
      <strong>Preparing research for {tickers || "this company"}…</strong>{" "}
      {p ? (
        working ? (
          <>Current stage: {p.current_stage ?? "starting"}{p.step_index != null && p.step_total != null ? ` (${p.step_index}/${p.step_total})` : ""}.</>
        ) : (
          <>Queued — {p.queue_depth} job{p.queue_depth === 1 ? "" : "s"} ahead in the preparation queue{p.worker_active ? "" : "; worker is idle"}.</>
        )
      ) : (
        <>Queued for preparation.</>
      )}{" "}
      It runs automatically; ask again in a few minutes.
    </div>
  );
}

function StatusNoticePanel({ notice }: { notice: ResearchQueryResponse }) {
  if (notice.status === "preparing") {
    const tickers = notice.preparing.map((p) => p.ticker).join(", ");
    return <PreparingNotice tickers={tickers} />;
  }
  if (notice.status === "company_not_found") {
    const tickers = notice.unresolved_tickers.join(", ");
    return (
      <div className="notice">
        {tickers || "That ticker"} doesn't look like a real, SEC-listed company.
      </div>
    );
  }
  if (notice.status === "research_failed") {
    return <div className="notice">Research preparation failed for this company.</div>;
  }
  return null;
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
  const [activeItem, setActiveItem] = useState<AIResearchHistoryItem | null>(null);
  const [statusNotice, setStatusNotice] = useState<ResearchQueryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [historyKey, setHistoryKey] = useState(0);

  const ask = async (q: string) => {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    setStatusNotice(null);
    try {
      const response = await api.researchQuery(q, contextTicker ?? undefined);
      if (response.status === "completed" || response.status === "insufficient_evidence") {
        // Re-fetch the row that was just persisted -- the active-answer
        // panel always renders a real AIResearchHistoryItem, whether it
        // was just generated or restored from history, so the two paths
        // never drift.
        const items = await api.getResearchHistory({
          ticker: contextTicker ?? undefined,
          limit: 1,
        });
        if (items[0]) setActiveItem(items[0]);
        setHistoryKey((k) => k + 1);
      } else {
        // preparing / company_not_found / research_failed -- nothing was
        // persisted (see api/routers/research.py::research_query), so
        // there's no history row to show; only the honest status notice.
        setActiveItem(null);
        setStatusNotice(response);
      }
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
          which tools were called and how it was verified, not just the final text. Answers are
          saved as real research history.
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
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
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
              disabled={loading}
            >
              {q}
            </button>
          ))}
        </div>
      </div>

      {error && <div className="notice">{error}</div>}
      {statusNotice && <StatusNoticePanel notice={statusNotice} />}

      <HistoryPanel
        key={historyKey}
        ticker={contextTicker}
        activeId={activeItem?.id ?? null}
        onSelect={setActiveItem}
        onDeleted={(id) => {
          if (activeItem?.id === id) setActiveItem(null);
        }}
      />

      {activeItem && <AnswerPanel item={activeItem} />}
    </div>
  );
}
