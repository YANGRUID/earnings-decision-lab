import { dataStateLabel, formatMoney, formatPlainPercent, formatRelativeTime } from "../../lib/format";
import type { ResearchOverview } from "../../types/api";

const DATE_SOURCE_LABELS: Record<string, string> = {
  alpha_vantage: "confirmed by provider",
  manual: "manual override",
  estimated: "estimated",
  unknown: "unconfirmed",
};

function nextEarningsLabel(overview: ResearchOverview): string {
  const estimate = overview.latest_earnings_estimate;
  if (!estimate?.estimated_report_date) {
    return "Not confirmed — no scheduled date on record from any provider";
  }
  const date = new Date(estimate.estimated_report_date).toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
  const source = DATE_SOURCE_LABELS[estimate.date_source] ?? estimate.date_source;
  return `${date} (${source})`;
}

export function OverviewTab({ overview }: { overview: ResearchOverview }) {
  const { company, latest_earnings_estimate: estimate, latest_volatility_snapshot: volatility } = overview;
  const hasEnoughForThesis = overview.earnings_events_count > 0 && overview.filings_count > 0;

  return (
    <div>
      <div className="grid grid-2">
        <div className="card">
          <h2>Company</h2>
          {company && (
            <>
              <div className="stat" style={{ marginBottom: 10 }}>
                <span className="stat-label">Name</span>
                <span className="stat-value small">{company.name}</span>
              </div>
              <div className="grid grid-2">
                <div className="stat">
                  <span className="stat-label">Sector</span>
                  <span className="stat-value small">{company.sector ?? "Unknown"}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">Exchange</span>
                  <span className="stat-value small">{company.exchange ?? "Unknown"}</span>
                </div>
              </div>
            </>
          )}
        </div>

        <div className="card">
          <h2>Upcoming earnings</h2>
          <div className="stat" style={{ marginBottom: 10 }}>
            <span className="stat-label">Next earnings</span>
            <span className="stat-value small">{nextEarningsLabel(overview)}</span>
          </div>
          <div className="grid grid-2">
            <div className="stat">
              <span className="stat-label">Price</span>
              <span className="stat-value small">
                {overview.latest_price ? `$${formatMoney(overview.latest_price)}` : "No price data"}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">Options status</span>
              <span className="stat-value small">{dataStateLabel(overview.options_data_state)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Implied move</span>
              <span className="stat-value small">
                {volatility ? formatPlainPercent(volatility.implied_move_pct) : "Not available yet"}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">Historical avg move</span>
              <span className="stat-value small">
                {overview.historical_moves
                  ? formatPlainPercent(overview.historical_moves.average_abs_move_pct)
                  : "No reported history yet"}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-2">
        <div className="card">
          <h2>Analyst consensus</h2>
          {estimate ? (
            <div className="grid grid-2">
              <div className="stat">
                <span className="stat-label">EPS estimate</span>
                <span className="stat-value small">
                  {estimate.eps_estimate_average ? formatMoney(estimate.eps_estimate_average) : "—"}
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">EPS revision trend</span>
                <span className="stat-value small mono">{estimate.eps_revision_direction}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Analyst count</span>
                <span className="stat-value small">{estimate.eps_estimate_analyst_count ?? "—"}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Source</span>
                <span className="stat-value small">{estimate.source_provider}</span>
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted" style={{ margin: 0 }}>
              No analyst consensus collected yet.
            </p>
          )}
        </div>

        <div className="card">
          <h2>AI Earnings Thesis</h2>
          <p className="text-sm text-muted" style={{ marginTop: 0 }}>
            {hasEnoughForThesis
              ? "Generated on demand from real filings and historical earnings data — see the AI Thesis tab."
              : "Not enough real data on record yet (needs reported earnings history and SEC filings) — a thesis generated now would be sparse."}
          </p>
          {overview.latest_job && (
            <p className="text-sm text-muted" style={{ margin: 0 }}>
              Last preparation run: <span className="mono">{overview.latest_job.status}</span>
              {overview.latest_job.completed_at &&
                ` · ${formatRelativeTime(overview.latest_job.completed_at)}`}
              {overview.latest_job.error && ` · ${overview.latest_job.error}`}
            </p>
          )}
        </div>
      </div>

      <details className="card">
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>
          Data on record (diagnostics)
        </summary>
        <ul className="freshness-list" style={{ marginBottom: 0 }}>
          <li>{overview.earnings_events_count} reported earnings events</li>
          <li>{overview.price_bars_count} daily price bars</li>
          <li>{overview.filings_count} SEC filings</li>
          <li>{overview.filing_chunks_count} searchable filing excerpts</li>
        </ul>
      </details>
    </div>
  );
}
