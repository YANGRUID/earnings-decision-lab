import { Link } from "react-router-dom";
import { useAsync } from "../../hooks/useAsync";
import { api } from "../../api/client";
import { formatMoney, formatPercent } from "../../lib/format";
import { LoadingState } from "../StatusStates";
import type { ResearchOverview } from "../../types/api";

function formatPlainPercent(value: string | null): string {
  if (value === null) return "—";
  return formatPercent(Number(value), 1);
}

export function UpcomingEarningsTab({
  ticker,
  overview,
}: {
  ticker: string;
  overview: ResearchOverview;
}) {
  const replay = useAsync(() => api.getReplaySummary(), []);
  const est = overview.latest_earnings_estimate;
  const iv = overview.latest_volatility_snapshot;
  const companyReplay = replay.data?.companies.find((c) => c.company.ticker === ticker);
  const hist = companyReplay?.historical_moves ?? null;

  return (
    <div>
      <p className="text-sm text-muted" style={{ marginTop: 0 }}>
        What the market currently expects for {ticker}'s <strong>next, unreported</strong>{" "}
        earnings report — separate from any specific past event. See{" "}
        <Link to="#historical">Historical Events</Link> for what actually happened before.
      </p>

      <div className="grid grid-2">
        <div className="card">
          <h2>Market expectations</h2>
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
                  <span className="stat-label">Revenue estimate (avg)</span>
                  <span className="stat-value small">
                    {est.revenue_estimate_average
                      ? `$${(Number(est.revenue_estimate_average) / 1e9).toFixed(2)}B`
                      : "—"}
                  </span>
                </div>
              </div>
              <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
                Estimated report date: {est.estimated_report_date ?? "unknown"} · consensus as of{" "}
                {new Date(est.snapshot_timestamp).toLocaleDateString()} ({est.source_provider}).
              </p>
            </>
          ) : (
            <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
              No real analyst-consensus snapshot has been collected yet. Use{" "}
              <span className="mono">Refresh</span> above to fetch one.
            </p>
          )}
        </div>

        <div className="card">
          <h2>Options market pricing</h2>
          {iv ? (
            <>
              <div className="grid grid-2" style={{ gap: 10 }}>
                <div className="stat">
                  <span className="stat-label">Implied move</span>
                  <span className="stat-value small">
                    {formatPlainPercent(iv.implied_move_pct)}
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
                Method: {iv.method} · computed {new Date(iv.computed_at).toLocaleString()}.
              </p>
            </>
          ) : (
            <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
              No real options-chain data has been ingested for {ticker} yet.
            </p>
          )}
        </div>
      </div>

      <div className="card">
        <h2>Historical move compatibility check</h2>
        {replay.loading && <LoadingState label="Loading historical moves…" />}
        {hist ? (
          <>
            <div className="grid grid-2" style={{ gap: 10 }}>
              <div className="stat">
                <span className="stat-label">Average |move| ({hist.sample_size} past events)</span>
                <span className="stat-value small">{formatPlainPercent(hist.average_abs_move_pct)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Largest past move</span>
                <span className="stat-value small">{formatPlainPercent(hist.largest_move_pct_signed)}</span>
              </div>
            </div>
            {iv?.implied_move_pct && (
              <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
                The current implied move ({formatPlainPercent(iv.implied_move_pct)}) compares to a
                real historical average of {formatPlainPercent(hist.average_abs_move_pct)} across{" "}
                {hist.sample_size} past reports — not a prediction, just how this compares to what
                actually happened before. See each candidate's own compatibility check in{" "}
                <strong>Strategy Lab</strong>.
              </p>
            )}
          </>
        ) : (
          !replay.loading && (
            <p className="text-sm text-muted" style={{ margin: 0 }}>
              No other reported event for {ticker} with a recorded move yet.
            </p>
          )
        )}
      </div>
    </div>
  );
}
