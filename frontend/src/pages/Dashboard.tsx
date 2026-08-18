import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { dataStateLabel, formatMoney, formatPlainPercent, formatRelativeTime } from "../lib/format";
import type { ResearchOverview } from "../types/api";

function normalizeTicker(raw: string): string {
  return raw.trim().toUpperCase();
}

const DATE_SOURCE_LABELS: Record<string, string> = {
  alpha_vantage: "confirmed",
  manual: "manual override",
  estimated: "estimated",
  unknown: "unconfirmed",
};

function nextEarningsLabel(overview: ResearchOverview): string {
  const estimate = overview.latest_earnings_estimate;
  if (!estimate?.estimated_report_date) return "Not confirmed";
  const date = new Date(estimate.estimated_report_date).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
  const source = DATE_SOURCE_LABELS[estimate.date_source] ?? estimate.date_source;
  return `${date} (${source})`;
}

async function fetchResearchedOverviews(): Promise<ResearchOverview[]> {
  const companies = await api.listCompanies();
  return Promise.all(companies.map((c) => api.getResearchOverview(c.ticker)));
}

export function Dashboard() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const overviews = useAsync(fetchResearchedOverviews, []);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const ticker = normalizeTicker(query);
    if (!ticker) return;
    navigate(`/company/${ticker}`);
  };

  return (
    <div>
      <div className="search-hero">
        <h1>Research any earnings event</h1>
        <p className="text-muted">
          Search a US-listed ticker to prepare real research, understand what's priced in ahead
          of its next earnings report, and compare deterministic options strategies against it.
          A personal research tool — not investment advice.
        </p>
        <form className="search-hero-form" onSubmit={submit}>
          <input
            className="search-hero-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search a ticker — e.g. NVDA, AAPL, COST…"
            autoFocus
            aria-label="Search ticker"
          />
          <button className="btn" type="submit">
            Research
          </button>
        </form>
      </div>

      <div className="page-header" style={{ marginTop: 8 }}>
        <h2 style={{ fontSize: 15, textTransform: "uppercase", letterSpacing: "0.03em" }}>
          Already researched
        </h2>
      </div>

      {overviews.loading && !overviews.data && <LoadingState label="Loading researched companies…" />}
      {overviews.error && !overviews.data && <ErrorState message={overviews.error} />}

      {overviews.data && overviews.data.length === 0 && (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            Nothing researched yet — search a ticker above to get started. Preparing a company
            pulls real historical earnings, price history, SEC filings, analyst estimates, and
            (where available) real options-chain data.
          </p>
        </div>
      )}

      {overviews.data && overviews.data.length > 0 && (
        <div className="grid grid-3">
          {overviews.data.filter((o) => o.company !== null).map((overview) => {
            const company = overview.company;
            if (!company) return null;
            return (
              <Link
                key={company.ticker}
                to={`/company/${company.ticker}`}
                className="card ticker-card"
              >
                <div className="ticker-card-symbol">{company.ticker}</div>
                <div className="ticker-card-name">{company.name}</div>
                <div className="grid grid-2" style={{ gap: 8, marginTop: 10 }}>
                  <div className="stat">
                    <span className="stat-label">Next earnings</span>
                    <span className="stat-value small">{nextEarningsLabel(overview)}</span>
                  </div>
                  <div className="stat">
                    <span className="stat-label">Price</span>
                    <span className="stat-value small">
                      {overview.latest_price ? `$${formatMoney(overview.latest_price)}` : "No price data"}
                    </span>
                  </div>
                  <div className="stat">
                    <span className="stat-label">Implied move</span>
                    <span className="stat-value small">
                      {overview.latest_volatility_snapshot
                        ? formatPlainPercent(overview.latest_volatility_snapshot.implied_move_pct)
                        : "Not available"}
                    </span>
                  </div>
                  <div className="stat">
                    <span className="stat-label">Historical avg move</span>
                    <span className="stat-value small">
                      {overview.historical_moves
                        ? formatPlainPercent(overview.historical_moves.average_abs_move_pct)
                        : "No history yet"}
                    </span>
                  </div>
                  <div className="stat">
                    <span className="stat-label">Options status</span>
                    <span className="stat-value small">{dataStateLabel(overview.options_data_state)}</span>
                  </div>
                  <div className="stat">
                    <span className="stat-label">Last refresh</span>
                    <span className="stat-value small">
                      {overview.latest_job?.completed_at
                        ? formatRelativeTime(overview.latest_job.completed_at)
                        : "Never"}
                    </span>
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
