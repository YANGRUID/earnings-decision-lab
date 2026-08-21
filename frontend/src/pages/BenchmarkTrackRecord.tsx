import { useState } from "react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import type { Rate, RiskProfile } from "../types/api";

function pct(rate: Rate): string {
  if (rate.pct === null) return "—";
  return `${(Number(rate.pct) * 100).toFixed(0)}%`;
}

function money(value: string | null): string {
  if (value === null) return "—";
  return `$${Number(value).toFixed(2)}`;
}

function num(value: string | null, digits = 2): string {
  if (value === null) return "—";
  return Number(value).toFixed(digits);
}

function RateRow({ label, rate }: { label: string; rate: Rate }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{pct(rate)}</span>
      <span className="text-sm text-faint">
        {rate.total === 0
          ? "N = 0"
          : `${rate.correct} of ${rate.total} settled decisions (N = ${rate.total})`}
      </span>
    </div>
  );
}

const CONFIDENCE_BUCKETS = ["<60%", "60-70%", "70-80%", "80-90%", "90%+"];
const DTE_BUCKETS = ["0-3", "4-7", "8-14", "15-30", "30+"];
const RISK_PROFILES: RiskProfile[] = ["conservative", "moderate", "aggressive"];

export function BenchmarkTrackRecord() {
  const [strategy, setStrategy] = useState("");
  const [confidenceBucket, setConfidenceBucket] = useState("");
  const [dteBucket, setDteBucket] = useState("");
  const [riskProfile, setRiskProfile] = useState<RiskProfile | "">("");
  const [ivRegime, setIvRegime] = useState("");

  const record = useAsync(
    () =>
      api.getBenchmarkTrackRecord({
        strategy: strategy.trim() || undefined,
        confidenceBucket: confidenceBucket || undefined,
        dteBucket: dteBucket || undefined,
        riskProfile: riskProfile || undefined,
        ivRegime: ivRegime.trim() || undefined,
      }),
    [strategy, confidenceBucket, dteBucket, riskProfile, ivRegime]
  );
  const calibration = useAsync(() => api.getBenchmarkCalibration(), []);

  if (record.loading && !record.data) {
    return <LoadingState label="Loading benchmark track record…" />;
  }
  if (record.error && !record.data) return <ErrorState message={record.error} />;
  if (!record.data) return null;

  const r = record.data;
  const filtered =
    strategy.trim() || confidenceBucket || dteBucket || riskProfile || ivRegime.trim();

  return (
    <div>
      <div className="page-header">
        <h1>AI Earnings Analyst Track Record</h1>
        <p>
          Verified performance of the real $2,000 Moderate AI Benchmark Portfolio — computed only
          over decisions with a real, captured entry and a real, captured exit (Phase 4.4/4.5).
          Historical performance does not imply future results.
        </p>
      </div>

      <div
        className="card"
        style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}
      >
        <input
          type="text"
          placeholder="Filter by strategy (e.g. iron_condor)"
          value={strategy}
          onChange={(e) => setStrategy(e.target.value)}
          style={{ maxWidth: 220 }}
        />
        <select value={confidenceBucket} onChange={(e) => setConfidenceBucket(e.target.value)}>
          <option value="">Any confidence</option>
          {CONFIDENCE_BUCKETS.map((b) => (
            <option key={b} value={b}>
              {b}
            </option>
          ))}
        </select>
        <select value={dteBucket} onChange={(e) => setDteBucket(e.target.value)}>
          <option value="">Any DTE</option>
          {DTE_BUCKETS.map((b) => (
            <option key={b} value={b}>
              {b} DTE
            </option>
          ))}
        </select>
        <select
          value={riskProfile}
          onChange={(e) => setRiskProfile(e.target.value as RiskProfile | "")}
        >
          <option value="">Any risk profile</option>
          {RISK_PROFILES.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="IV regime (high / normal / low)"
          value={ivRegime}
          onChange={(e) => setIvRegime(e.target.value)}
          style={{ maxWidth: 200 }}
        />
      </div>

      <div className="card">
        <h2>Benchmark Summary</h2>
        <div className="grid grid-3" style={{ gap: 16 }}>
          <div className="stat">
            <span className="stat-label">Total Decisions</span>
            <span className="stat-value">{r.total_decisions}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Settled Decisions</span>
            <span className="stat-value">{r.settled_decisions}</span>
          </div>
        </div>
        {filtered && (
          <p className="text-sm text-faint" style={{ marginBottom: 0, marginTop: 8 }}>
            Filtered to the criteria above — clear a filter to see the whole portfolio again.
          </p>
        )}
      </div>

      {r.settled_decisions === 0 ? (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            No settled trades available. Metrics appear once at least one decision has a real,
            captured exit.
          </p>
        </div>
      ) : (
        <>
          <div className="card">
            <h2>Performance Metrics</h2>
            <div className="grid grid-3" style={{ gap: 16 }}>
              <RateRow label="Win Rate" rate={r.win_rate} />
              <div className="stat">
                <span className="stat-label">Average R</span>
                <span className="stat-value">{num(r.average_r)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Median R</span>
                <span className="stat-value">{num(r.median_r)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Expectancy</span>
                <span className="stat-value">{num(r.expectancy)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Profit Factor</span>
                <span className="stat-value">{num(r.profit_factor)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Max Drawdown</span>
                <span className="stat-value">{money(r.max_drawdown)}</span>
                <span className="text-sm text-faint">
                  {r.max_drawdown_pct !== null
                    ? `${num(r.max_drawdown_pct, 1)}% of peak equity`
                    : ""}
                </span>
              </div>
            </div>
          </div>

          <div className="grid grid-3" style={{ gap: 16 }}>
            <div className="card">
              <RateRow label="Directional Accuracy" rate={r.directional_accuracy} />
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Did the underlying move in the predicted direction.
              </p>
            </div>
            <div className="card">
              <RateRow label="Breakeven Accuracy" rate={r.breakeven_accuracy} />
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Did the real exit price clear (or stay within) the strategy's own breakeven.
              </p>
            </div>
            <div className="card">
              <RateRow label="Range Accuracy" rate={r.range_accuracy} />
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Did the real move stay inside the option market's own implied move at decision
                time.
              </p>
            </div>
          </div>
        </>
      )}

      {calibration.data && calibration.data.settled_decisions > 0 && (
        <div className="card">
          <h2>Probability Calibration</h2>
          <table>
            <thead>
              <tr>
                <th>Predicted probability</th>
                <th>N</th>
                <th>Realized win rate</th>
              </tr>
            </thead>
            <tbody>
              {calibration.data.buckets.map((b) => (
                <tr key={b.label}>
                  <td className="mono">{b.label}</td>
                  <td className="mono">{b.rate.total}</td>
                  <td className="mono">{b.rate.total > 0 ? pct(b.rate) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
            Whether the AI's stated probability actually correlates with the real settlement
            outcome — buckets with very few decisions are not statistically meaningful yet.
          </p>
        </div>
      )}
      {calibration.data && calibration.data.settled_decisions === 0 && (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            No settled trades available for probability calibration yet.
          </p>
        </div>
      )}
    </div>
  );
}
