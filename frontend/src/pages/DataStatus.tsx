import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { LoadingState } from "../components/StatusStates";

export function DataStatus() {
  const companies = useAsync(() => api.listCompanies(), []);
  const earnings = useAsync(() => api.listEarnings({ limit: 100 }), []);

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
        <h2>Known gaps — not hidden, not faked</h2>
        <ul className="text-sm">
          <li>No live options-chain data (implied move, IV, strategy replay) — no provider configured yet</li>
          <li>No analyst consensus estimates</li>
          <li>No earnings-call transcripts (no legally accessible free source identified)</li>
          <li>Evaluation framework (Phase 9) not built yet — no evaluation metrics are shown because none have been measured</li>
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
