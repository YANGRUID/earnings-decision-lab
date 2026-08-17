import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { MovePill } from "../components/MovePill";
import { formatMoney, formatPercent } from "../lib/format";

function formatPlainPercent(value: string | null): string {
  if (value === null) return "—";
  return formatPercent(Number(value), 1);
}

export function EarningsEvent() {
  const { id = "" } = useParams();
  const eventId = Number(id);
  const event = useAsync(() => api.getEarningsEvent(eventId), [eventId]);
  const history = useAsync(
    () => (event.data ? api.listEarnings({ ticker: event.data.company.ticker, limit: 8 }) : Promise.resolve([])),
    [event.data?.company.ticker],
  );
  // Forward-looking data (next-period estimate, implied move) is
  // company-level and always-current -- never tied to this specific past
  // event -- so it's fetched separately from GET /research/{ticker}/overview
  // rather than living on the event's own response. See types/api.ts.
  const overview = useAsync(
    () => (event.data ? api.getResearchOverview(event.data.company.ticker) : Promise.resolve(null)),
    [event.data?.company.ticker],
  );

  if (event.loading) return <LoadingState label="Loading earnings event…" />;
  if (event.error) return <ErrorState message={event.error} />;
  if (!event.data) return null;

  const e = event.data;
  const est = overview.data?.latest_earnings_estimate ?? null;
  const iv = overview.data?.latest_volatility_snapshot ?? null;
  const hist = e.historical_moves;

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
          <h2>Historical moves — {e.company.ticker}</h2>
          {hist ? (
            <div className="grid grid-2" style={{ gap: 10 }}>
              <div className="stat">
                <span className="stat-label">Average |move| ({hist.sample_size} events)</span>
                <MovePill value={hist.average_abs_move_pct} />
              </div>
              <div className="stat">
                <span className="stat-label">Largest move</span>
                <MovePill value={hist.largest_move_pct_signed} />
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted">
              No other reported event for {e.company.ticker} with a recorded move yet.
            </p>
          )}
        </div>
      </div>

      <p className="text-sm text-muted" style={{ marginTop: 4 }}>
        The two cards below are about {e.company.ticker}'s <em>next</em> earnings report — a
        separate, always-current concern from the FY{e.fiscal_year} Q{e.fiscal_quarter} event
        above, not a property of it.
      </p>
      <div className="grid grid-2">
        <div className="card">
          <h2>{e.company.ticker}'s upcoming earnings — market expectations</h2>
          {est ? (
            <>
              <div className="grid grid-2" style={{ gap: 10 }}>
                <div className="stat">
                  <span className="stat-label">EPS estimate (avg, {est.horizon})</span>
                  <span className="stat-value small">{formatMoney(est.eps_estimate_average)}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">EPS revision trend</span>
                  <span className="stat-value small mono">{est.eps_revision_direction}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">Analyst count (EPS)</span>
                  <span className="stat-value small">{est.eps_estimate_analyst_count ?? "—"}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">Revenue estimate (avg, {est.horizon})</span>
                  <span className="stat-value small">
                    {est.revenue_estimate_average
                      ? `$${(Number(est.revenue_estimate_average) / 1e9).toFixed(2)}B`
                      : "—"}
                  </span>
                </div>
              </div>
              <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
                Figures above are for Alpha Vantage's "{est.horizon}" consensus specifically —
                not necessarily a single quarter's worth if this is a fiscal year-end period.
                Estimated report date: {est.estimated_report_date ?? "unknown"} · consensus as of{" "}
                {new Date(est.snapshot_timestamp).toLocaleDateString()} ({est.source_provider}).
              </p>
            </>
          ) : (
            <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
              No analyst-consensus snapshot has been collected yet for {e.company.ticker}'s next
              unreported period. See <Link to="/data-status">Data / Eval Status</Link>.
            </p>
          )}
        </div>

        <div className="card">
          <h2>{e.company.ticker}'s upcoming earnings — implied move</h2>
          {iv ? (
            <>
              <div className="grid grid-2" style={{ gap: 10 }}>
                <div className="stat">
                  <span className="stat-label">Implied move</span>
                  <span className="stat-value small">
                    {iv.implied_move_pct ? formatPlainPercent(iv.implied_move_pct) : "—"}
                    {iv.implied_move_absolute ? ` ($${formatMoney(iv.implied_move_absolute)})` : ""}
                  </span>
                </div>
                <div className="stat">
                  <span className="stat-label">Expiration used</span>
                  <span className="stat-value small mono">{iv.near_term_expiration ?? "—"}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">ATM IV</span>
                  <span className="stat-value small">{formatPlainPercent(iv.atm_iv_near)}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">Put/call OI ratio</span>
                  <span className="stat-value small">
                    {iv.put_call_open_interest_ratio
                      ? Number(iv.put_call_open_interest_ratio).toFixed(2)
                      : "—"}
                  </span>
                </div>
              </div>
              <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
                Method: {iv.method} · computed {new Date(iv.computed_at).toLocaleString()}. See{" "}
                <a
                  href="https://github.com/YANGRUID/earnings-decision-lab/blob/main/docs/options_methodology.md"
                  target="_blank"
                  rel="noreferrer"
                >
                  docs/options_methodology.md
                </a>
                .
              </p>
            </>
          ) : (
            <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
              No options-chain data has been ingested for {e.company.ticker} yet — Alpha Vantage's
              options endpoints are premium-gated on this project's plan (see{" "}
              <Link to="/historical-replay">Historical Replay</Link> and{" "}
              <Link to="/data-status">Data / Eval Status</Link>). Use{" "}
              <Link to="/options-lab">Options Lab</Link> to price a hypothetical strategy with your
              own strikes and premiums instead.
            </p>
          )}
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
        <h2>AI research</h2>
        <p className="text-sm text-muted">
          Ask <Link to="/research">AI Research</Link> about {e.company.ticker}'s filings —
          e.g. "What did {e.company.ticker} say about demand in its most recent risk factors?"
        </p>
      </div>
    </div>
  );
}
