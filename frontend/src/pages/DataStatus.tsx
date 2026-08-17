import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { formatPercent } from "../lib/format";

function Timestamp({ value }: { value: string | null }) {
  if (value === null) return <span className="text-faint">never</span>;
  return <span className="mono">{new Date(value).toLocaleString()}</span>;
}

export function DataStatus() {
  const status = useAsync(() => api.getSystemStatus(), []);

  if (status.loading) return <LoadingState label="Loading system status…" />;
  if (status.error) return <ErrorState message={status.error} />;
  if (!status.data) return null;

  const { counts, freshness, llm, embedding_model: embeddingModel, evaluation } = status.data;

  return (
    <div>
      <div className="page-header">
        <h1>Data / Evaluation Status</h1>
        <p>What has real data behind it right now, stated plainly rather than left to be discovered.</p>
      </div>

      <div className="card">
        <h2>Live counts</h2>
        <div className="grid grid-4" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">Companies</span>
            <span className="stat-value">{counts.companies}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Earnings events</span>
            <span className="stat-value">{counts.earnings_events}</span>
          </div>
          <div className="stat">
            <span className="stat-label">— with reported results</span>
            <span className="stat-value">{counts.earnings_events_with_results}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Price bars</span>
            <span className="stat-value">{counts.price_bars.toLocaleString()}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Filings</span>
            <span className="stat-value">{counts.filings}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Filing chunks (RAG)</span>
            <span className="stat-value">{counts.document_chunks.toLocaleString()}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Earnings estimate snapshots</span>
            <span className="stat-value">{counts.earnings_estimate_snapshots}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Options snapshots</span>
            <span className="stat-value">{counts.options_snapshots}</span>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Data freshness</h2>
        <div className="grid grid-2" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">Latest price bar</span>
            <span className="stat-value small">
              {freshness.latest_price_bar_date ?? <span className="text-faint">—</span>}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Latest filing ingested</span>
            <span className="stat-value small">
              <Timestamp value={freshness.latest_filing_retrieved_at} />
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Latest earnings estimate snapshot</span>
            <span className="stat-value small">
              <Timestamp value={freshness.latest_earnings_estimate_snapshot_at} />
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Latest options-chain snapshot</span>
            <span className="stat-value small">
              <Timestamp value={freshness.latest_options_snapshot_at} />
            </span>
          </div>
        </div>
        {freshness.latest_options_snapshot_at === null && (
          <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
            No options-chain data ingested yet — Alpha Vantage's options endpoints are
            premium-gated on this project's plan. See{" "}
            <a
              href="https://github.com/YANGRUID/earnings-decision-lab/blob/main/docs/data_sources.md"
              target="_blank"
              rel="noreferrer"
            >
              docs/data_sources.md
            </a>
            .
          </p>
        )}
      </div>

      <div className="card">
        <h2>AI provider</h2>
        <div className="grid grid-3" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">LLM provider</span>
            <span className="stat-value small mono">{llm.provider}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Model</span>
            <span className="stat-value small mono">{llm.model ?? "—"}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Status</span>
            <span className={`pill ${llm.configured ? "pill-positive" : "pill-negative"}`}>
              {llm.configured ? "configured" : "not configured"}
            </span>
          </div>
        </div>
        <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
          Embeddings: <span className="mono">{embeddingModel}</span> (local, no API key needed).
        </p>
      </div>

      <div className="card">
        <h2>Evaluation</h2>
        {!evaluation.available || !evaluation.run ? (
          <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
            No evaluation run recorded on this deployment. Results are real output written to a
            local file, not committed to the repo — see docs/evaluation.md for the most recent
            measured run and how to reproduce it.
          </p>
        ) : (
          <>
            <p className="text-sm text-muted">
              Run at {new Date(evaluation.run.run_at).toLocaleString()} against{" "}
              {evaluation.run.llm_provider}/{evaluation.run.llm_model}. Full methodology and
              honest analysis of these numbers in docs/evaluation.md.
            </p>
            <div className="grid grid-4">
              {evaluation.run.retrieval && (
                <div className="stat">
                  <span className="stat-label">
                    Retrieval Recall@5 ({evaluation.run.retrieval.item_count} items)
                  </span>
                  <span className="stat-value">
                    {formatPercent(evaluation.run.retrieval.mean_recall_at_5)}
                  </span>
                </div>
              )}
              {evaluation.run.rag_answer && (
                <div className="stat">
                  <span className="stat-label">
                    RAG answer fact coverage ({evaluation.run.rag_answer.item_count} items)
                  </span>
                  <span className="stat-value">
                    {formatPercent(evaluation.run.rag_answer.mean_fact_coverage)}
                  </span>
                </div>
              )}
              {evaluation.run.agent && (
                <div className="stat">
                  <span className="stat-label">
                    Agent tool-selection accuracy ({evaluation.run.agent.item_count} items)
                  </span>
                  <span className="stat-value">
                    {formatPercent(evaluation.run.agent.tool_selection_accuracy)}
                  </span>
                </div>
              )}
              {evaluation.run.extraction && (
                <div className="stat">
                  <span className="stat-label">
                    Extraction capex accuracy ({evaluation.run.extraction.item_count} items)
                  </span>
                  <span className="stat-value">
                    {formatPercent(evaluation.run.extraction.capex_accuracy)}
                  </span>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      <details className="card">
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>Known gaps — not hidden, not faked</summary>
        <ul className="text-sm" style={{ marginTop: 10 }}>
          <li>
            No real options-chain data — Alpha Vantage's options endpoints are premium-gated on
            this project's plan (confirmed live, see docs/data_sources.md); implied move, IV, and
            put/call ratios stay null until a subscription exists or forward snapshots accumulate.
          </li>
          <li>No earnings-call transcripts (no legally accessible free source identified)</li>
          <li>
            Retrieval quality is the measured bottleneck for AI Research — see docs/evaluation.md
            for the specific, verified cause
          </li>
        </ul>
        <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
          Full detail in{" "}
          <a
            href="https://github.com/YANGRUID/earnings-decision-lab/blob/main/docs/limitations.md"
            target="_blank"
            rel="noreferrer"
          >
            docs/limitations.md
          </a>
          .
        </p>
      </details>
    </div>
  );
}
