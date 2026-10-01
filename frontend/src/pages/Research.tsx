import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { Markdown } from "../components/Markdown";
import { dataStateLabel, formatRelativeTime, providerLabel } from "../lib/format";
import type {
  AgentWorkflow,
  AIResearchHistoryItem,
  ResearchOverview,
  ResearchQueryResponse,
} from "../types/api";

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

// Section 10-12 -- the answer experience: company, as-of, freshness,
// grounding status, human-readable evidence categories and filing
// references, with retrieval internals only under Advanced.
const TOOL_CATEGORY: Record<string, string> = {
  earnings_history: "Earnings history",
  filings_search: "SEC filings",
  guidance_comparison: "Guidance comparison",
  options_snapshot: "Options market snapshot",
  strategy_replay: "Historical price reaction",
  company_fundamentals: "Fundamentals",
};

function filingCategory(filingType: string): string {
  const t = filingType.toUpperCase();
  if (t.startsWith("10-K")) return "10-K";
  if (t.startsWith("10-Q")) return "10-Q";
  if (t.startsWith("8-K")) return "8-K";
  return t;
}

// Requirement 64 -- plain language for the normal reader. Nobody outside
// this repository needs to know a node is called "evidence_quality_gate";
// they need to know the system checked whether it had enough to answer.
// The node names themselves stay available under Advanced details.
const WORKFLOW_STEP_LABEL: Record<string, string> = {
  classify_intent: "Understanding the question",
  window_context: "Checking research freshness",
  plan_research: "Choosing sources",
  execute_tools: "Collecting evidence",
  merge_evidence: "Assembling evidence",
  evidence_quality_gate: "Checking evidence coverage",
  targeted_retrieve: "Retrying missing evidence",
  synthesize: "Writing the answer",
  verify: "Verification",
  revise: "Revising unsupported claims",
};

const EVIDENCE_CATEGORY_LABEL: Record<string, string> = {
  filing: "SEC filings",
  earnings_history: "earnings history",
  estimates: "analyst estimates",
  guidance: "guidance",
  options_context: "options data",
  derived: "calculations",
};

function categoryList(categories: string[]): string {
  const readable = categories.map((c) => EVIDENCE_CATEGORY_LABEL[c] ?? c.replace(/_/g, " "));
  if (readable.length <= 1) return readable.join("");
  return `${readable.slice(0, -1).join(", ")} and ${readable[readable.length - 1]}`;
}

const QUALITY_PILL: Record<string, { cls: string; label: string }> = {
  sufficient: { cls: "pill pill-positive", label: "Evidence covered the question" },
  partial: { cls: "pill pill-neutral", label: "Partial evidence" },
  insufficient: { cls: "pill pill-warning", label: "Not enough evidence on record" },
};

function WorkflowPanel({ workflow }: { workflow: AgentWorkflow }) {
  const quality = workflow.evidence_quality;
  // Deduplicated in order: a node that ran twice (the gate, or verify
  // after a revision) is one step that happened twice, not two steps.
  const steps: { label: string; runs: typeof workflow.node_runs }[] = [];
  for (const run of workflow.node_runs) {
    const label = WORKFLOW_STEP_LABEL[run.node] ?? run.node.replace(/_/g, " ");
    const existing = steps.find((s) => s.label === label);
    if (existing) existing.runs.push(run);
    else steps.push({ label, runs: [run] });
  }
  return (
    <div className="card" data-testid="research-workflow">
      <h2>How this answer was reached</h2>
      {quality && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span className={QUALITY_PILL[quality.status].cls} data-testid="evidence-quality-status">
            {QUALITY_PILL[quality.status].label}
          </span>
          <span className="text-muted text-sm">{quality.explanation}</span>
        </div>
      )}
      {workflow.retrieval_rounds > 0 && (
        <p className="text-sm text-muted" style={{ margin: "10px 0 0" }}>
          The first pass left a gap, so {categoryList(quality?.recommended_retrieval ?? [])} was
          searched again before the answer was written.
        </p>
      )}
      {quality?.conflicts.map((c, i) => (
        <p className="text-sm" key={i} style={{ margin: "8px 0 0" }} data-testid="evidence-conflict">
          <span className="pill pill-warning">Evidence disagrees</span>{" "}
          <span className="text-muted">{c.description}</span>
        </p>
      ))}
      {workflow.warnings.map((w, i) => (
        <p className="text-sm text-muted" key={i} style={{ margin: "8px 0 0" }}>{w}</p>
      ))}
      <div style={{ marginTop: 14 }}>
        {steps.map((step) => (
          <div className="trace-step" key={step.label}>
            <div className="trace-step-header">
              <span className="trace-step-name">
                {step.label}
                {step.runs.length > 1 ? ` · ran ${step.runs.length}×` : ""}
              </span>
              <span className={`pill ${stepPill(step.runs)}`}>
                {stepLabel(step.runs)} ·{" "}
                {step.runs.reduce((total, r) => total + r.duration_ms, 0).toFixed(0)}ms
              </span>
            </div>
          </div>
        ))}
      </div>
      {workflow.errors.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>What did not work</h3>
          {workflow.errors.map((e, i) => (
            <p className="text-sm text-muted" key={i} style={{ margin: "4px 0 0" }}>
              {WORKFLOW_STEP_LABEL[e.node] ?? e.node}: {e.message}
            </p>
          ))}
        </>
      )}
    </div>
  );
}

function stepPill(runs: { status: string }[]): string {
  if (runs.some((r) => r.status === "failed")) return "pill-negative";
  if (runs.some((r) => r.status === "degraded")) return "pill-warning";
  if (runs.every((r) => r.status === "skipped")) return "pill-neutral";
  return "pill-positive";
}

function stepLabel(runs: { status: string }[]): string {
  if (runs.some((r) => r.status === "failed")) return "failed";
  if (runs.some((r) => r.status === "degraded")) return "degraded";
  if (runs.every((r) => r.status === "skipped")) return "not needed";
  return "done";
}

function AnswerPanel({ item, workflow }: { item: AIResearchHistoryItem; workflow?: AgentWorkflow | null }) {
  const filingCats = Array.from(new Set(item.citations.map((c) => filingCategory(c.filing_type))));
  const toolCats = Array.from(new Set(item.tool_calls.filter((t) => t.success).map((t) => TOOL_CATEGORY[t.tool_name] ?? t.tool_name.replace(/_/g, " "))));
  const grounding = item.verification_ran
    ? item.verification_supported ? { cls: "pill pill-positive", label: "Grounded — supported by evidence" }
      : { cls: "pill pill-warning", label: "Revised after verification" }
    : item.citations.length > 0 ? { cls: "pill pill-neutral", label: "Cited — verification not run" }
      : { cls: "pill pill-warning", label: "No filing citations" };
  return (
    <>
      <div className="card" data-testid="answer-header">
        <div className="grid grid-4" style={{ gap: 10 }}>
          <div className="stat"><span className="stat-label">Detected company</span><span className="stat-value small mono">{item.ticker ?? "—"}</span></div>
          <div className="stat"><span className="stat-label">As of</span><span className="stat-value small mono">{new Date(item.created_at).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} ET</span></div>
          <div className="stat"><span className="stat-label">Research freshness</span><span className="stat-value small">{formatRelativeTime(item.created_at)}</span></div>
          <div className="stat"><span className="stat-label">Grounding</span><span className={grounding.cls} data-testid="grounding-status">{grounding.label}</span></div>
        </div>
        <p className="text-muted" style={{ margin: "10px 0 0" }}>&ldquo;{item.question}&rdquo;</p>
      </div>

      <div className="card">
        <h2>Answer</h2>
        <Markdown>{item.answer_markdown}</Markdown>
      </div>

      <div className="card" data-testid="evidence-sources">
        <h2>Evidence sources</h2>
        {filingCats.length === 0 && toolCats.length === 0 ? (
          <p className="text-muted" style={{ margin: 0 }}>No external evidence was retrieved for this answer.</p>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {filingCats.map((c) => <span key={c} className="pill pill-neutral">{c}</span>)}
            {toolCats.map((c) => <span key={c} className="pill pill-neutral">{c}</span>)}
          </div>
        )}
        {item.citations.length > 0 && (
          <>
            <h3 style={{ marginTop: 12 }}>Filing references</h3>
            {item.citations.map((c) => (
              <div key={c.marker} className="source-item">
                <span className="citation-badge">{c.marker}</span>
                <div>
                  <div className="source-title">{c.ticker} · {filingCategory(c.filing_type)} · filed {c.filing_date}{c.section ? ` · ${c.section}` : ""}</div>
                  <a className="text-link text-sm" href={c.source_url} target="_blank" rel="noreferrer">Inspect source</a>
                </div>
              </div>
            ))}
          </>
        )}
        {item.tool_calls.some((t) => t.tool_name === "earnings_history" && t.success) && (
          <p className="text-faint text-sm" style={{ margin: "8px 0 0" }}>Earnings-history evidence was used; see the company workspace's Earnings Setup tab for the underlying data.</p>
        )}
      </div>

      {workflow && <WorkflowPanel workflow={workflow} />}

      <details className="card">
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>Advanced details</summary>
        <div className="grid grid-3" style={{ marginTop: 14, marginBottom: 14 }}>
          <div className="stat"><span className="stat-label">Intent</span><span className="stat-value small">{item.intent_category}</span></div>
          <div className="stat"><span className="stat-label">Planning</span><span className="stat-value small">{item.planning_method}</span></div>
          <div className="stat"><span className="stat-label">Provider / model</span><span className="stat-value small">{providerLabel(item.provider)} · {item.model}</span></div>
          <div className="stat"><span className="stat-label">Duration</span><span className="stat-value small">{(item.total_duration_ms / 1000).toFixed(1)}s</span></div>
          <div className="stat"><span className="stat-label">Tokens (in/out)</span><span className="stat-value small">{item.total_input_tokens} / {item.total_output_tokens}</span></div>
          <div className="stat"><span className="stat-label">Est. cost</span><span className="stat-value small">{item.estimated_cost_usd ? `$${Number(item.estimated_cost_usd).toFixed(4)}` : "n/a"}</span></div>
        </div>
        {workflow && (
          <div className="grid grid-3" style={{ marginBottom: 14 }} data-testid="workflow-diagnostics">
            <div className="stat"><span className="stat-label">Agent runtime</span><span className="stat-value small mono">{workflow.runtime_version}</span></div>
            <div className="stat"><span className="stat-label">Graph version</span><span className="stat-value small mono">{workflow.graph_version}</span></div>
            <div className="stat"><span className="stat-label">Run id</span><span className="stat-value small mono">{workflow.run_id}</span></div>
            <div className="stat"><span className="stat-label">LLM calls</span><span className="stat-value small">{workflow.llm_calls}</span></div>
            <div className="stat"><span className="stat-label">Retrieval rounds</span><span className="stat-value small">{workflow.retrieval_rounds}</span></div>
            <div className="stat"><span className="stat-label">Revisions</span><span className="stat-value small">{workflow.revision_count}</span></div>
          </div>
        )}
        {workflow && (
          <details style={{ marginBottom: 14 }}>
            <summary className="text-sm text-faint" style={{ cursor: "pointer" }}>Workflow node timings</summary>
            {workflow.node_runs.map((n, i) => (
              <div className="trace-step" key={i}>
                <div className="trace-step-header">
                  <span className="trace-step-name mono">{n.node}{n.attempt > 1 ? ` (attempt ${n.attempt})` : ""}</span>
                  <span className="pill pill-neutral">{n.status} · {n.duration_ms.toFixed(0)}ms · {n.llm_calls} llm · {n.tool_calls} tools</span>
                </div>
              </div>
            ))}
            <p className="text-faint text-sm" style={{ margin: "8px 0 0" }}>
              Checkpoint: {workflow.checkpoint_thread_id ?? "not persisted for this run"}
            </p>
          </details>
        )}
        {item.tool_calls.length === 0 ? (
          <p className="text-sm text-muted">No tools were needed for this question.</p>
        ) : (
          item.tool_calls.map((tc, i) => (
            <div className="trace-step" key={i}>
              <div className="trace-step-header">
                <span className="trace-step-name">{TOOL_CATEGORY[tc.tool_name] ?? tc.tool_name}</span>
                <span className={`pill ${tc.success ? "pill-positive" : "pill-negative"}`}>{tc.success ? "ok" : "failed"} · {tc.duration_ms.toFixed(0)}ms</span>
              </div>
              <div className="text-muted" style={{ marginTop: 4 }}>{tc.summary || tc.error}</div>
              {tc.query_description && (
                <details style={{ marginTop: 6 }}>
                  <summary className="text-sm text-faint" style={{ cursor: "pointer" }}>Retrieval query</summary>
                  <pre className="mono text-sm" style={{ whiteSpace: "pre-wrap", marginTop: 4 }}>{tc.query_description}</pre>
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
          <><strong>Running</strong> — current step: {p.current_stage ?? "starting"}{p.step_index != null && p.step_total != null ? ` (${p.step_index}/${p.step_total})` : ""}.{p.heartbeat_seconds_ago != null ? ` Last progress ${p.heartbeat_seconds_ago}s ago.` : ""}{p.completed ? ` ${p.completed} stage${p.completed === 1 ? "" : "s"} completed.` : ""}</>
        ) : (
          <><strong>Queued</strong> — {p.queue_depth} job{p.queue_depth === 1 ? "" : "s"} ahead in the preparation queue{p.worker_active ? "" : "; worker is idle"}.</>
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
  // The workflow belongs to the live run, not to the persisted row (which
  // predates Phase LG-1 and is unchanged). Held separately and cleared the
  // moment a different answer is selected, so a workflow is never shown
  // next to an answer it did not produce.
  const [activeWorkflow, setActiveWorkflow] = useState<AgentWorkflow | null>(null);
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
        setActiveWorkflow(response.workflow);
        setHistoryKey((k) => k + 1);
      } else {
        // preparing / company_not_found / research_failed -- nothing was
        // persisted (see api/routers/research.py::research_query), so
        // there's no history row to show; only the honest status notice.
        setActiveItem(null);
        setActiveWorkflow(null);
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
        onSelect={(item) => {
          setActiveItem(item);
          setActiveWorkflow(null);
        }}
        onDeleted={(id) => {
          if (activeItem?.id === id) {
            setActiveItem(null);
            setActiveWorkflow(null);
          }
        }}
      />

      {activeItem && <AnswerPanel item={activeItem} workflow={activeWorkflow} />}
    </div>
  );
}
