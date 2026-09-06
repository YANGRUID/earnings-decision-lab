import { useAsync } from "../hooks/useAsync";
import { api } from "../api/client";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { configLabel } from "../components/v4/shared";
import type { V4ChallengerOutcomeStats } from "../types/api";

// V4.2 CHALLENGER forward track record.
//
// A SEPARATE cohort. Nothing on this page is summed with, subtracted from or
// displayed beside a V4.1 number, because the two are not interchangeable and
// a reader who sees them side by side will compare them whether or not the
// sample supports it. The page says CHALLENGER and PARALLEL SHADOW in its own
// header for the same reason.
//
// The primary unit is the EVENT. Configuration statistics appear underneath,
// clearly labelled as sizings of the same forecast rather than independent
// observations.

const GRADE_LABEL: Record<string, string> = {
  EXECUTABLE_BID_ASK: "Executable bid/ask",
  MARKET_CLOSE_FALLBACK: "End-of-day closing mark",
  EXPIRATION_INTRINSIC_AT_CLOSE: "Expiration intrinsic value",
  UNRESOLVED: "Unresolved",
};

const GRADE_PILL: Record<string, string> = {
  EXECUTABLE_BID_ASK: "pill-positive",
  MARKET_CLOSE_FALLBACK: "pill-warning",
  EXPIRATION_INTRINSIC_AT_CLOSE: "pill-warning",
  UNRESOLVED: "pill-neutral",
};

function pct(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function OutcomePanel({
  title,
  caption,
  stats,
}: {
  title: string;
  caption: string;
  stats: V4ChallengerOutcomeStats;
}) {
  return (
    <div className="card" style={{ margin: 0 }} data-testid={`outcomes-${title.toLowerCase().replace(/\s+/g, "-")}`}>
      <h3 style={{ marginTop: 0 }}>{title}</h3>
      <p className="text-faint text-sm">{caption}</p>
      {stats.settled === 0 ? (
        <div className="empty-state" style={{ padding: "12px 0" }}>
          <strong>Nothing settled yet.</strong> No realized challenger outcome exists.
        </div>
      ) : (
        <table style={{ fontVariantNumeric: "tabular-nums" }}>
          <tbody>
            <tr><td>Settled</td><td className="mono">{stats.settled}</td></tr>
            <tr><td>Wins / losses</td><td className="mono">{stats.wins} / {stats.losses}</td></tr>
            <tr><td>Win rate</td><td className="mono">{pct(stats.win_rate)}</td></tr>
            <tr>
              <td>Median return on standardized capital</td>
              <td className="mono">{pct(stats.median_standardized_return)}</td>
            </tr>
            <tr>
              <td>Median return on capital used</td>
              <td className="mono">{pct(stats.median_capital_used_return)}</td>
            </tr>
          </tbody>
        </table>
      )}
    </div>
  );
}

export function V4ChallengerTrackRecord() {
  const record = useAsync(() => api.getV4ChallengerTrackRecord(), []);
  const operations = useAsync(() => api.getV4ChallengerOperations(), []);

  if (record.loading && !record.data) {
    return <LoadingState label="Loading challenger track record…" />;
  }
  if (record.error && !record.data) return <ErrorState message={record.error} />;
  if (!record.data) return null;

  const data = record.data;
  const enabled = operations.data?.scheduler.parallel_enabled ?? false;
  const reasons = Object.entries(data.events.no_action_reasons);
  const configs = Object.entries(data.by_configuration);
  const ladder = Object.entries(data.by_expiry_ladder_position);

  return (
    <div>
      <div className="page-header">
        <h1>Challenger Track Record</h1>
        <span className="pill pill-warning" data-testid="challenger-label">
          V4.2 CHALLENGER · PARALLEL SHADOW
        </span>
      </div>
      <div className="notice notice-warning" data-testid="challenger-notice">{data.notice}</div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Parallel shadow state</h2>
        <div className="grid grid-3" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">Challenger observation</span>
            <span className={`pill ${enabled ? "pill-positive" : "pill-neutral"}`}>
              {enabled ? "ENABLED" : "DISABLED"}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Official methodology</span>
            <span className="stat-value">V4.1 CONTROL</span>
          </div>
          <div className="stat">
            <span className="stat-label">Runs as</span>
            <span className="stat-value text-sm">
              A phase of the V4.1 window, after the control
            </span>
          </div>
        </div>
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Events</h2>
        <p className="text-faint text-sm">
          The primary unit. Six configurations of one event are six sizings of the same
          forecast, not six forecasts.
        </p>
        <div className="grid grid-4" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">Observed</span>
            <span className="stat-value mono">{data.events.observed}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Action</span>
            <span className="stat-value mono">{data.events.action}</span>
          </div>
          <div className="stat">
            <span className="stat-label">No action</span>
            <span className="stat-value mono">{data.events.no_action}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Action rate</span>
            <span className="stat-value mono">
              {data.events.action_rate === null ? "—" : pct(data.events.action_rate)}
            </span>
          </div>
        </div>
        {reasons.length > 0 ? (
          <>
            <h3>Why the challenger declined</h3>
            <p className="text-faint text-sm">
              NO ACTION is a successful methodology outcome, not a failure. These are the
              reasons it gave.
            </p>
            <table style={{ fontVariantNumeric: "tabular-nums" }}>
              <thead><tr><th>Reason</th><th>Events</th></tr></thead>
              <tbody>
                {reasons.map(([reason, count]) => (
                  <tr key={reason}>
                    <td>{reason}</td>
                    <td className="mono">{count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : null}
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Position lifecycle</h2>
        <div className="grid grid-4" style={{ gap: 10 }}>
          {(
            [
              ["Entries observed", data.lifecycle.entries_observed],
              ["Entries failed", data.lifecycle.entries_failed],
              ["Awaiting settlement", data.lifecycle.settlements_due],
              ["Settled", data.lifecycle.settled],
            ] as [string, number][]
          ).map(([label, value]) => (
            <div className="stat" key={label}>
              <span className="stat-label">{label}</span>
              <span className="stat-value mono">{value}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-2" style={{ gap: 12 }}>
        <OutcomePanel
          title="All outcomes"
          caption="Every settlement of record, however it was priced."
          stats={data.all_outcomes}
        />
        <OutcomePanel
          title="Executable only"
          caption="Only outcomes whose exit value could genuinely have been transacted on every leg. An analytics filter, never a deletion."
          stats={data.executable_only_outcomes}
        />
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Settlement quality</h2>
        <p className="text-faint text-sm">
          A closing mark is not a fill. An end-of-day or expiration-intrinsic settlement is
          real evidence, but it is not evidence that the position could have been closed at
          that price.
        </p>
        <div className="grid grid-4" style={{ gap: 10 }}>
          {Object.entries(data.settlement_quality).map(([grade, count]) => (
            <div className="stat" key={grade}>
              <span className="stat-label">{GRADE_LABEL[grade] ?? grade}</span>
              <span className={`pill ${GRADE_PILL[grade] ?? "pill-neutral"}`}>{count}</span>
            </div>
          ))}
        </div>
      </div>

      {configs.length > 0 ? (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>By configuration</h2>
          <p className="text-faint text-sm">
            Reported separately from the event counts above, and never added to them.
          </p>
          <div style={{ overflowX: "auto" }}>
            <table style={{ fontVariantNumeric: "tabular-nums" }}>
              <thead>
                <tr>
                  <th>Configuration</th><th>Action</th><th>No action</th>
                  <th>Settled</th><th>Median return</th>
                </tr>
              </thead>
              <tbody>
                {configs.map(([key, row]) => (
                  <tr key={key}>
                    <td>{configLabel(key)}</td>
                    <td className="mono">{row.action ?? 0}</td>
                    <td className="mono">{row.no_action ?? 0}</td>
                    <td className="mono">{row.settled ?? 0}</td>
                    <td className="mono">{pct(row.median_standardized_return as string | null)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {ladder.length > 0 ? (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>By expiry ladder position</h2>
          <p className="text-faint text-sm">
            Rung 0 is the expiry V4.1 would have chosen. Later rungs are the alternatives the
            challenger compared it against on the same T+1 objective.
          </p>
          <table style={{ fontVariantNumeric: "tabular-nums" }}>
            <thead><tr><th>Rung</th><th>Settled</th><th>Wins</th><th>Losses</th></tr></thead>
            <tbody>
              {ladder.map(([rung, row]) => (
                <tr key={rung}>
                  <td className="mono">{rung}</td>
                  <td className="mono">{row.settled ?? 0}</td>
                  <td className="mono">{row.wins ?? 0}</td>
                  <td className="mono">{row.losses ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {data.warnings.length > 0 ? (
        <div className="card" data-testid="tiny-n-warnings">
          <h2 style={{ marginTop: 0 }}>Read this before drawing a conclusion</h2>
          <ul>
            {data.warnings.map((warning) => (
              <li key={warning} className="text-sm">{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
