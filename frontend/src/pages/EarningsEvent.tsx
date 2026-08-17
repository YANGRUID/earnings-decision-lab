import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { MovePill } from "../components/MovePill";
import { formatMoney } from "../lib/format";

export function EarningsEvent() {
  const { id = "" } = useParams();
  const eventId = Number(id);
  const event = useAsync(() => api.getEarningsEvent(eventId), [eventId]);
  const history = useAsync(
    () => (event.data ? api.listEarnings({ ticker: event.data.company.ticker, limit: 8 }) : Promise.resolve([])),
    [event.data?.company.ticker],
  );

  if (event.loading) return <LoadingState label="Loading earnings event…" />;
  if (event.error) return <ErrorState message={event.error} />;
  if (!event.data) return null;

  const e = event.data;

  return (
    <div>
      <div className="page-header">
        <h1>
          {e.company.ticker} — FY{e.fiscal_year} Q{e.fiscal_quarter}
        </h1>
        <p>
          {e.earnings_date ?? "Date unconfirmed"}
          {e.date_confirmed ? " · confirmed via 8-K Item 2.02" : " · not yet confirmed"}
        </p>
      </div>

      <div className="grid grid-3">
        <div className="card">
          <h2>Actual results</h2>
          {e.result ? (
            <div className="grid grid-2" style={{ gap: 10 }}>
              <div className="stat">
                <span className="stat-label">EPS</span>
                <span className="stat-value small">{formatMoney(e.result.actual_eps)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Revenue</span>
                <span className="stat-value small">
                  {e.result.actual_revenue
                    ? `$${(Number(e.result.actual_revenue) / 1e9).toFixed(2)}B`
                    : "—"}
                </span>
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted">No reported actuals yet.</p>
          )}
        </div>

        <div className="card">
          <h2>Price reaction</h2>
          {e.price_reaction ? (
            <div className="grid grid-2" style={{ gap: 10 }}>
              <div className="stat">
                <span className="stat-label">Next-day move</span>
                <MovePill value={e.price_reaction.next_day_move_pct} />
              </div>
              <div className="stat">
                <span className="stat-label">5-day move</span>
                <MovePill value={e.price_reaction.five_day_move_pct} />
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted">No price reaction recorded yet.</p>
          )}
        </div>

        <div className="card">
          <h2>Market expectations</h2>
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            No options-chain provider is configured yet, so implied move / ATM IV / consensus
            estimates aren't available for this event. See{" "}
            <Link to="/data-status">Data / Eval Status</Link>.
          </p>
        </div>
      </div>

      <div className="card">
        <h2>Recent earnings moves — {e.company.ticker}</h2>
        {history.loading && <LoadingState />}
        {history.data && (
          <table>
            <thead>
              <tr>
                <th>Period</th>
                <th>Date</th>
                <th>EPS</th>
              </tr>
            </thead>
            <tbody>
              {history.data.map((h) => (
                <tr key={h.id}>
                  <td>
                    <Link to={`/earnings/${h.id}`}>
                      FY{h.fiscal_year} Q{h.fiscal_quarter}
                    </Link>
                  </td>
                  <td className="mono">{h.earnings_date ?? "—"}</td>
                  <td className="mono">{h.id === e.id ? "(this event)" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h2>Options</h2>
        <p className="text-sm text-muted">
          No historical option chain exists for this event to replay a strategy against. Use{" "}
          <Link to="/options-lab">Options Lab</Link> to price a hypothetical strategy with your
          own strikes and premiums.
        </p>
      </div>

      <div className="card">
        <h2>AI research</h2>
        <p className="text-sm text-muted">
          Ask <Link to="/research">AI Research</Link> about {e.company.ticker}'s filings —
          e.g. "What did {e.company.ticker} say about demand in its most recent risk factors?"
        </p>
      </div>
    </div>
  );
}
