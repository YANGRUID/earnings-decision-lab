import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { MovePill } from "../components/MovePill";
import { TickerSearchBar } from "../components/TickerSearchBar";

export function HistoricalReplay() {
  const replay = useAsync(() => api.getReplaySummary(), []);

  if (replay.loading) return <LoadingState label="Loading historical replay…" />;
  if (replay.error) return <ErrorState message={replay.error} />;
  if (!replay.data) return null;

  const { companies, options_data_ingested: optionsDataIngested } = replay.data;

  return (
    <div>
      <div className="page-header">
        <h1>Historical Replay</h1>
        <p>
          Real historical price moves, and real implied-vs-realized comparisons where a forward
          options snapshot exists — never a fabricated options-chain reconstruction.
        </p>
      </div>

      <TickerSearchBar />

      <div className="card">
        <h2>Why there's no options-chain replay yet</h2>
        <p className="text-sm text-muted">
          Alpha Vantage's <span className="mono">HISTORICAL_OPTIONS</span> endpoint requires a
          premium subscription this project doesn't have — confirmed with a live check, not
          assumed (see{" "}
          <a
            href="https://github.com/YANGRUID/earnings-decision-lab/blob/main/docs/data_sources.md"
            target="_blank"
            rel="noreferrer"
          >
            docs/data_sources.md
          </a>
          ). So there's no way to reconstruct a real options strategy against a past earnings
          event today. Instead, this page shows the historical price moves that{" "}
          <em>are</em> real, and will start showing real implied-vs-realized move comparisons as
          forward options snapshots (collected ahead of each real upcoming earnings date) age
          into reported events — no code change needed when that happens.
        </p>
        {!optionsDataIngested && (
          <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
            No options-chain data has been ingested for any company yet, so every
            implied-vs-realized section below is currently empty.
          </p>
        )}
      </div>

      {companies.map((entry) => (
        <div className="card" key={entry.company.ticker}>
          <h2>
            <Link to={`/company/${entry.company.ticker}`} style={{ color: "var(--color-accent)" }}>
              {entry.company.ticker}
            </Link>{" "}
            — {entry.company.name}
          </h2>

          {entry.historical_moves === null ? (
            <p className="text-sm text-muted">
              No reported earnings events with a recorded next-day move yet for this company.
            </p>
          ) : (
            <div className="grid grid-4" style={{ gap: 10, marginBottom: 16 }}>
              <div className="stat">
                <span className="stat-label">Reported events</span>
                <span className="stat-value small">{entry.historical_moves.sample_size}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Average |move|</span>
                <span className="stat-value small">
                  <MovePill value={entry.historical_moves.average_abs_move_pct} />
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">Median |move|</span>
                <span className="stat-value small">
                  <MovePill value={entry.historical_moves.median_abs_move_pct} />
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">Largest move</span>
                <span className="stat-value small">
                  <MovePill value={entry.historical_moves.largest_move_pct_signed} />
                </span>
              </div>
            </div>
          )}

          <h3 className="text-sm" style={{ marginBottom: 8 }}>
            Implied vs. realized
          </h3>
          {entry.implied_vs_realized.length === 0 ? (
            <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
              {optionsDataIngested
                ? "No forward snapshot for this company has reached a reported earnings date yet."
                : "Populates once options-chain data is ingested ahead of a real earnings date."}
            </p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Earnings date</th>
                  <th>Snapshot taken</th>
                  <th>Implied move</th>
                  <th>Realized next-day move</th>
                </tr>
              </thead>
              <tbody>
                {entry.implied_vs_realized.map((row) => (
                  <tr key={row.snapshot_timestamp}>
                    <td className="mono">{row.target_earnings_date}</td>
                    <td className="mono">{new Date(row.snapshot_timestamp).toLocaleDateString()}</td>
                    <td>
                      {row.implied_move_pct ? (
                        <MovePill value={row.implied_move_pct} />
                      ) : (
                        <span className="text-faint">—</span>
                      )}
                    </td>
                    <td>
                      <MovePill value={row.realized_next_day_move_pct} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}
    </div>
  );
}
