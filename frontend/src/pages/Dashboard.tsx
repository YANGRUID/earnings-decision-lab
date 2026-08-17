import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";

export function Dashboard() {
  const companies = useAsync(() => api.listCompanies(), []);

  return (
    <div>
      <div className="page-header">
        <h1>Dashboard</h1>
        <p>
          Point-in-time earnings intelligence and options analytics for a small set of covered
          tickers. This is a portfolio research project, not investment advice.
        </p>
      </div>

      {companies.loading && <LoadingState label="Loading covered companies…" />}
      {companies.error && <ErrorState message={companies.error} />}

      {companies.data && (
        <div className="grid grid-2">
          {companies.data.map((company) => (
            <Link key={company.ticker} to={`/company/${company.ticker}`} className="card">
              <h2 style={{ marginBottom: 6 }}>{company.ticker}</h2>
              <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 4 }}>{company.name}</div>
              <div className="text-muted text-sm">{company.sector ?? "—"}</div>
            </Link>
          ))}
        </div>
      )}

      <div className="card">
        <h2>What this covers</h2>
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          Real historical earnings results and price reactions, real SEC filing search with
          citations, a deterministic options strategy calculator, and an AI research assistant
          that plans and executes real tool calls rather than answering from memory. See{" "}
          <a href="/data-status">Data / Eval Status</a> for exactly what has real data behind it
          today.
        </p>
      </div>
    </div>
  );
}
