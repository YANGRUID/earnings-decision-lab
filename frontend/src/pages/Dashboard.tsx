import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";

function normalizeTicker(raw: string): string {
  return raw.trim().toUpperCase();
}

export function Dashboard() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const companies = useAsync(() => api.listCompanies(), []);

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

      {companies.loading && <LoadingState label="Loading researched companies…" />}
      {companies.error && <ErrorState message={companies.error} />}

      {companies.data && companies.data.length === 0 && (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            Nothing researched yet — search a ticker above to get started. Preparing a company
            pulls real historical earnings, price history, SEC filings, analyst estimates, and
            (where available) real options-chain data.
          </p>
        </div>
      )}

      {companies.data && companies.data.length > 0 && (
        <div className="grid grid-3">
          {companies.data.map((company) => (
            <Link key={company.ticker} to={`/company/${company.ticker}`} className="card ticker-card">
              <div className="ticker-card-symbol">{company.ticker}</div>
              <div className="ticker-card-name">{company.name}</div>
              <div className="text-muted text-sm">{company.sector ?? "Sector unknown"}</div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
