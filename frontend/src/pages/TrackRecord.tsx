import { useState } from "react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import type { Rate } from "../types/api";

function pct(rate: Rate): string {
  if (rate.pct === null) return "—";
  return `${(Number(rate.pct) * 100).toFixed(0)}%`;
}

function RateRow({ label, rate }: { label: string; rate: Rate }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{pct(rate)}</span>
      <span className="text-sm text-faint">
        {rate.total === 0 ? "N = 0" : `${rate.correct} of ${rate.total} final decisions (N = ${rate.total})`}
      </span>
    </div>
  );
}

export function TrackRecord() {
  const [window, setWindow] = useState<"all_time" | "last_10">("all_time");
  const [ticker, setTicker] = useState("");
  const record = useAsync(
    () => api.getTrackRecord({ window, ticker: ticker.trim() || undefined }),
    [window, ticker]
  );

  if (record.loading && !record.data) return <LoadingState label="Loading track record…" />;
  if (record.error && !record.data) return <ErrorState message={record.error} />;
  if (!record.data) return null;

  const r = record.data;

  return (
    <div>
      <div className="page-header">
        <h1>AI Decision Track Record</h1>
        <p>
          Honest reliability metrics for the AI Options Decision Engine — computed only over
          decisions with real, settled post-earnings outcomes. Historical accuracy does not imply
          future profitability. Some older decisions lack real point-in-time options pricing, so
          strategy-level P&amp;L statistics are only ever shown when valid option snapshots were
          actually captured — never estimated.
        </p>
      </div>

      <div className="card" style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 4 }}>
          <button
            className={`tab-button ${window === "all_time" ? "active" : ""}`}
            onClick={() => setWindow("all_time")}
          >
            All Time
          </button>
          <button
            className={`tab-button ${window === "last_10" ? "active" : ""}`}
            onClick={() => setWindow("last_10")}
          >
            Last 10 Decisions
          </button>
        </div>
        <input
          type="text"
          placeholder="Filter by ticker (optional)"
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          style={{ maxWidth: 220 }}
        />
      </div>

      {r.evaluated_count === 0 ? (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            No settled decisions yet{ticker ? ` for ${ticker}` : ""}. Metrics appear once real
            post-earnings price data exists for at least one generated decision.
          </p>
        </div>
      ) : (
        <>
          <div className="grid grid-3" style={{ gap: 16 }}>
            <div className="card">
              <RateRow label="Directional Accuracy" rate={r.directional_accuracy} />
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Did the stock move in the predicted direction.
              </p>
            </div>
            <div className="card">
              <RateRow label="Bullish-Decision Accuracy" rate={r.bullish_accuracy} />
            </div>
            <div className="card">
              <RateRow label="Bearish-Decision Accuracy" rate={r.bearish_accuracy} />
            </div>
            <div className="card">
              <RateRow label="Breakeven Success" rate={r.breakeven_success} />
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Did the underlying finish beyond the recommended strategy's breakeven.
              </p>
            </div>
            <div className="card">
              <RateRow label="Implied-Move-View Accuracy" rate={r.volatility_view_accuracy} />
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Long-vol calls correct when the actual move exceeded the implied move; short-vol
                calls correct when it did not.
              </p>
            </div>
            <div className="card">
              <RateRow label="High-Confidence Accuracy (≥70)" rate={r.high_confidence_accuracy} />
            </div>
          </div>

          <div className="card">
            <h2>Strategy Win Rate</h2>
            <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
              {r.strategy_win_rate_available
                ? "Available for decisions with real point-in-time option entry/exit prices."
                : "Not available — this project has not yet captured real point-in-time option " +
                  "entry/exit prices for any settled decision. Directional Accuracy and Breakeven " +
                  "Success above are real, distinct metrics and must not be read as a win rate."}
            </p>
          </div>

          <div className="card">
            <h2>Average Confidence</h2>
            <span className="stat-value">
              {r.average_confidence ? Number(r.average_confidence).toFixed(0) : "—"} / 100
            </span>
          </div>

          {r.confidence_calibration.some((b) => b.rate.total > 0) && (
            <div className="card">
              <h2>Confidence Calibration</h2>
              <table>
                <thead>
                  <tr>
                    <th>Confidence range</th>
                    <th>N</th>
                    <th>Realized directional accuracy</th>
                  </tr>
                </thead>
                <tbody>
                  {r.confidence_calibration.map((b) => (
                    <tr key={b.label}>
                      <td className="mono">{b.label}</td>
                      <td className="mono">{b.rate.total}</td>
                      <td className="mono">{b.rate.total > 0 ? pct(b.rate) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Whether higher stated confidence actually correlates with higher realized accuracy
                — buckets with very few decisions are not statistically meaningful yet.
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
