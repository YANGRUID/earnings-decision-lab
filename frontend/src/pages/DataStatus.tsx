import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import {
  DOMAIN_LABELS,
  formatPercent,
  formatRelativeTime,
  MARKET_SESSION_LABELS,
  providerLabel,
} from "../lib/format";
import type { DomainStatus } from "../types/api";

function Timestamp({ value }: { value: string | null }) {
  if (value === null) return <span className="text-faint">never</span>;
  return <span className="mono">{new Date(value).toLocaleString()}</span>;
}

function ProviderHealthRow({ domain }: { domain: DomainStatus }) {
  const errored = domain.providers.find((p) => p.last_error_status !== null);
  return (
    <tr>
      <td>{DOMAIN_LABELS[domain.domain] ?? domain.domain}</td>
      <td className="mono">
        {providerLabel(domain.primary)}
        {domain.primary_is_override && <span className="text-faint"> (override)</span>}
      </td>
      <td className="mono">
        {domain.fallback ? providerLabel(domain.fallback) : <span className="text-faint">none</span>}
      </td>
      <td>
        {errored ? (
          <span className="pill pill-negative">
            {providerLabel(errored.provider)}: {errored.last_error_status} (
            {formatRelativeTime(errored.last_error_at)})
          </span>
        ) : (
          <span className="pill pill-positive">no recent errors</span>
        )}
      </td>
    </tr>
  );
}

export function DataStatus() {
  const status = useAsync(() => api.getSystemStatus(), []);

  if (status.loading) return <LoadingState label="Loading system status…" />;
  if (status.error) return <ErrorState message={status.error} />;
  if (!status.data) return null;

  const {
    counts,
    freshness,
    llm,
    embedding_model: embeddingModel,
    evaluation,
    ibkr,
    market_session: marketSession,
    providers,
  } = status.data;

  return (
    <div>
      <div className="page-header">
        <h1>System Status</h1>
        <p>
          The technical control room for this deployment: provider health, connectivity, real
          data coverage, freshness, and evaluation results — stated plainly rather than left to
          be discovered. Change what's active in{" "}
          <Link to="/settings/providers">Settings → Data Providers</Link>.
        </p>
      </div>

      <div className="card">
        <h2>Market &amp; connectivity</h2>
        <div className="grid grid-3" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">US market session</span>
            <span className="stat-value small">
              {MARKET_SESSION_LABELS[marketSession] ?? marketSession}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">IBKR Gateway</span>
            <span
              className={`pill ${ibkr.gateway_reachable && ibkr.authenticated ? "pill-positive" : "pill-negative"}`}
            >
              {ibkr.gateway_reachable
                ? ibkr.authenticated
                  ? "running & authenticated"
                  : "running, not authenticated"
                : "offline"}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Manage connection</span>
            <Link to="/settings/ibkr" className="text-sm">
              Settings → IBKR
            </Link>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Provider health</h2>
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Domain</th>
                <th>Active</th>
                <th>Fallback</th>
                <th>Recent errors</th>
              </tr>
            </thead>
            <tbody>
              {providers.domains.map((d) => (
                <ProviderHealthRow key={d.domain} domain={d} />
              ))}
            </tbody>
          </table>
        </div>
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
            No options-chain data ingested yet for any company. Real options data requires either
            a configured Interactive Brokers Gateway connection or an Alpha Vantage plan with
            options entitlements (that endpoint is premium-gated on this project's current plan).
            See{" "}
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
          Switch providers in <Link to="/settings/ai-provider">Settings → AI Provider</Link>.
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
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>Coverage &amp; limitations</summary>
        <ul className="text-sm" style={{ marginTop: 10 }}>
          <li>
            Options-chain data (implied move, IV, put/call ratios) requires either a locally
            running, authenticated Interactive Brokers Gateway or an Alpha Vantage plan with
            options entitlements — Alpha Vantage's options endpoints are premium-gated on this
            project's current plan (confirmed live, see docs/data_sources.md); without either
            source configured, these fields stay null rather than estimated.
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
