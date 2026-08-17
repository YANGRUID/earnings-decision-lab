import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { LoadingState } from "../components/StatusStates";
import { formatPercent } from "../lib/format";

export function DataStatus() {
  const companies = useAsync(() => api.listCompanies(), []);
  const earnings = useAsync(() => api.listEarnings({ limit: 100 }), []);
  const evaluation = useAsync(() => api.getLatestEvaluation(), []);

  return (
    <div>
      <div className="page-header">
        <h1>Data / Evaluation Status</h1>
        <p>What has real data behind it today, stated plainly rather than left to be discovered.</p>
      </div>

      <div className="card">
        <h2>Live counts</h2>
        {companies.loading || earnings.loading ? (
          <LoadingState />
        ) : (
          <div className="grid grid-3">
            <div className="stat">
              <span className="stat-label">Covered companies</span>
              <span className="stat-value">{companies.data?.length ?? "—"}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Earnings events (sample)</span>
              <span className="stat-value">{earnings.data?.length ?? "—"}</span>
            </div>
          </div>
        )}
      </div>

      <div className="card">
        <h2>Real, working data</h2>
        <ul className="text-sm">
          <li>Historical earnings actuals (EPS, revenue) — SEC EDGAR XBRL, all 4 tickers</li>
          <li>Confirmed earnings dates — sourced from 8-K Item 2.02 filings, not guessed</li>
          <li>Daily price history and price reactions — Tiingo (fallback: Alpha Vantage)</li>
          <li>2,200+ real SEC filing chunks, hybrid-searchable with citations</li>
          <li>Structured guidance extraction with full provenance (source, model, prompt version)</li>
          <li>AI research assistant with real tool orchestration, verification, and execution traces</li>
        </ul>
      </div>

      <div className="card">
        <h2>Evaluation</h2>
        {evaluation.loading ? (
          <LoadingState />
        ) : !evaluation.data?.available || !evaluation.data.run ? (
          <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
            No evaluation run recorded on this deployment. Results are real output written to a
            local file, not committed to the repo — see docs/evaluation.md for the most recent
            measured run and how to reproduce it.
          </p>
        ) : (
          <>
            <p className="text-sm text-muted">
              Run at {new Date(evaluation.data.run.run_at).toLocaleString()} against{" "}
              {evaluation.data.run.llm_provider}/{evaluation.data.run.llm_model}. Full methodology
              and honest analysis of these numbers in docs/evaluation.md.
            </p>
            <div className="grid grid-4">
              {evaluation.data.run.retrieval && (
                <div className="stat">
                  <span className="stat-label">Retrieval Recall@5 ({evaluation.data.run.retrieval.item_count} items)</span>
                  <span className="stat-value">
                    {formatPercent(evaluation.data.run.retrieval.mean_recall_at_5)}
                  </span>
                </div>
              )}
              {evaluation.data.run.rag_answer && (
                <div className="stat">
                  <span className="stat-label">RAG answer fact coverage ({evaluation.data.run.rag_answer.item_count} items)</span>
                  <span className="stat-value">
                    {formatPercent(evaluation.data.run.rag_answer.mean_fact_coverage)}
                  </span>
                </div>
              )}
              {evaluation.data.run.agent && (
                <div className="stat">
                  <span className="stat-label">Agent tool-selection accuracy ({evaluation.data.run.agent.item_count} items)</span>
                  <span className="stat-value">
                    {formatPercent(evaluation.data.run.agent.tool_selection_accuracy)}
                  </span>
                </div>
              )}
              {evaluation.data.run.extraction && (
                <div className="stat">
                  <span className="stat-label">Extraction capex accuracy ({evaluation.data.run.extraction.item_count} items)</span>
                  <span className="stat-value">
                    {formatPercent(evaluation.data.run.extraction.capex_accuracy)}
                  </span>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      <div className="card">
        <h2>Known gaps — not hidden, not faked</h2>
        <ul className="text-sm">
          <li>No live options-chain data (implied move, IV, strategy replay) — no provider configured yet</li>
          <li>No analyst consensus estimates</li>
          <li>No earnings-call transcripts (no legally accessible free source identified)</li>
          <li>Retrieval quality is the measured bottleneck for AI Research (Recall@5 ≈ 0.35) — see docs/evaluation.md for the specific, verified cause</li>
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
      </div>
    </div>
  );
}
