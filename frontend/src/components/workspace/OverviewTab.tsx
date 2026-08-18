import type { ResearchOverview } from "../../types/api";

function freshnessLabel(count: number, noun: string): string {
  return count > 0 ? `${count} ${noun}` : `No ${noun} yet`;
}

export function OverviewTab({ overview }: { overview: ResearchOverview }) {
  const { company } = overview;
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
          <h2>Data on record</h2>
          <ul className="freshness-list">
            <li>{freshnessLabel(overview.earnings_events_count, "reported earnings events")}</li>
            <li>{freshnessLabel(overview.price_bars_count, "daily price bars")}</li>
            <li>{freshnessLabel(overview.filings_count, "SEC filings")}</li>
            <li>{freshnessLabel(overview.filing_chunks_count, "searchable filing excerpts")}</li>
            <li>
              {overview.latest_earnings_estimate
                ? "Real analyst consensus for next earnings"
                : "No analyst consensus yet"}
            </li>
            <li>
              {overview.latest_volatility_snapshot
                ? "Real options-implied move computed"
                : "No options-implied move yet"}
            </li>
          </ul>
        </div>
      </div>

      {overview.latest_job && (
        <div className="card">
          <h2>Last preparation run</h2>
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            Status <span className="mono">{overview.latest_job.status}</span> · started{" "}
            {new Date(overview.latest_job.started_at).toLocaleString()}
            {overview.latest_job.completed_at &&
              ` · completed ${new Date(overview.latest_job.completed_at).toLocaleString()}`}
            {overview.latest_job.error && ` · ${overview.latest_job.error}`}
          </p>
        </div>
      )}
    </div>
  );
}
