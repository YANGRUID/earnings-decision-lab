import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState, EmptyState } from "../components/StatusStates";

export function Company() {
  const { ticker = "" } = useParams();
  const company = useAsync(() => api.getCompany(ticker), [ticker]);
  const earnings = useAsync(() => api.listEarnings({ ticker, limit: 12 }), [ticker]);

  return (
    <div>
      <div className="page-header">
        <h1>{ticker}</h1>
        {company.data && <p>{company.data.name}</p>}
      </div>

      {company.error && <ErrorState message={company.error} />}

      <div className="card">
        <h2>Earnings history</h2>
        {earnings.loading && <LoadingState />}
        {earnings.error && <ErrorState message={earnings.error} />}
        {earnings.data && earnings.data.length === 0 && (
          <EmptyState>No earnings events on record for this ticker.</EmptyState>
        )}
        {earnings.data && earnings.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Period</th>
                <th>Earnings date</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {earnings.data.map((event) => (
                <tr key={event.id}>
                  <td>
                    <Link to={`/earnings/${event.id}`}>
                      FY{event.fiscal_year} Q{event.fiscal_quarter}
                    </Link>
                  </td>
                  <td className="mono">{event.earnings_date ?? "—"}</td>
                  <td>
                    {event.date_confirmed ? (
                      <span className="pill pill-neutral">confirmed</span>
                    ) : (
                      <span className="pill pill-neutral">unconfirmed</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h2>Ask about {ticker}</h2>
        <p className="text-sm text-muted">
          Use <Link to="/research">AI Research</Link> to ask questions grounded in this
          company's real filings and earnings history, with citations.
        </p>
      </div>
    </div>
  );
}
