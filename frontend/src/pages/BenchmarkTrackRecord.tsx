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
// V4.1 methodology foundation (2026-08-31) -- the only two real engine
// versions this codebase has ever written to DecisionSnapshot.engine_version
// (services/decision_snapshot_freezing.py::ENGINE_VERSION, analytics/
// decision/v4_methodology.py::ENGINE_VERSION_V4). V4 has zero official
// decisions today -- selecting it is expected to show an honest empty
// state, never a fabricated row.
const ENGINE_COHORTS = [
  { value: "", label: "All Engines" },
  { value: "options-decision-engine-v3", label: "V3" },
  { value: "options-decision-engine-v4", label: "V4" },
];

export function BenchmarkTrackRecord() {
  const [strategy, setStrategy] = useState("");
  const [confidenceBucket, setConfidenceBucket] = useState("");
  const [dteBucket, setDteBucket] = useState("");
  const [riskProfile, setRiskProfile] = useState<RiskProfile | "">("");
  const [ivRegime, setIvRegime] = useState("");
  const [engineVersion, setEngineVersion] = useState("");

  const record = useAsync(
    () =>
      api.getBenchmarkTrackRecord({
        strategy: strategy.trim() || undefined,
        confidenceBucket: confidenceBucket || undefined,
        dteBucket: dteBucket || undefined,
        riskProfile: riskProfile || undefined,
        ivRegime: ivRegime.trim() || undefined,
        engineVersion: engineVersion || undefined,
      }),
    [strategy, confidenceBucket, dteBucket, riskProfile, ivRegime, engineVersion]
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
        <h1>V3 Historical Control</h1>
        <div className="notice" data-testid="v3-control-notice">
          <strong>Historical control cohort.</strong> Legacy V3 methodology, observed at 15:55 ET
          (<span className="mono">v3-pre-earnings-1555et-v1</span>). This is not the current V4
          decision engine; it is retained as the benchmark V4 is forward-tested against.
        </div>
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
        <select value={engineVersion} onChange={(e) => setEngineVersion(e.target.value)}>
          {ENGINE_COHORTS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
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
            <span className="stat-label">Decisions Generated</span>
            <span className="stat-value">{r.total_decisions}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Actionable Decisions</span>
            <span className="stat-value">{r.actionable_decisions}</span>
            <span className="text-sm text-faint">A real strategy was recommended</span>
          </div>
          <div className="stat">
            <span className="stat-label">No-Action Decisions</span>
            <span className="stat-value">{r.no_action_decisions}</span>
            <span className="text-sm text-faint">The engine found nothing actionable</span>
          </div>
          <div className="stat">
            <span className="stat-label">Entries Captured</span>
            <span className="stat-value">{r.entries_captured}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Entry Capture Failed</span>
            <span className="stat-value">{r.entries_capture_failed}</span>
            <span className="text-sm text-faint">Of actionable decisions only</span>
          </div>
          <div className="stat">
            <span className="stat-label">Settled</span>
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
            {engineVersion === "options-decision-engine-v4"
              ? "V4 has zero official decisions today — it is experimental and disabled for " +
                "official trading (see the V4.1 methodology foundation). This honest empty " +
                "state is expected, not an error."
              : "No settled trades available. Performance metrics (win rate, R-multiples, " +
                "accuracy) appear only once at least one decision has a real, captured exit " +
                "— the counts above are real and honest in the meantime, they just aren't a " +
                "performance outcome yet."}
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
                <span className="stat-label">
                  {r.legacy_capital_caveat ? "V3 Legacy Aggregate Loss" : "Max Drawdown"}
                </span>
                <span className="stat-value">{money(r.max_drawdown)}</span>
                <span className="text-sm text-faint">
                  {r.legacy_capital_caveat
                    ? "Not comparable as a portfolio drawdown percentage — see below"
                    : r.max_drawdown_pct !== null
                      ? `${num(r.max_drawdown_pct, 1)}% of peak equity`
                      : ""}
                </span>
              </div>
            </div>
            {/* Post-official-run cleanup (2026-08-27), Section 6 --
                communication only, no metric here changed. */}
            <p className="text-sm text-faint" style={{ marginTop: 12, marginBottom: 0 }}>
              R-multiples are realized: computed from an actual exit at executable bid/ask before
              expiration, not a strategy's theoretical max loss/profit at expiration. Early-exit
              spread cost can push a realized loss past the defined-risk maximum shown at entry.
            </p>
            {r.legacy_capital_caveat && (
              <p className="text-sm text-faint" style={{ marginTop: 8, marginBottom: 0 }}>
                {r.legacy_capital_caveat}
              </p>
            )}
          </div>

          <div className="card">
            <h2>Standardized Per-Decision Metrics</h2>
            <p className="text-sm text-faint" style={{ marginTop: 0 }}>
              Each decision graded independently against the same $2,000 standardized capital —
              not a shared portfolio, so this deliberately never shows a portfolio drawdown.
            </p>
            <div className="grid grid-3" style={{ gap: 16 }}>
              <div className="stat">
                <span className="stat-label">N</span>
                <span className="stat-value">{r.standardized.n}</span>
                <span className="text-sm text-faint">
                  {r.standardized.wins} wins / {r.standardized.losses} losses
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">Mean Return on Standardized Capital</span>
                <span className="stat-value">
                  {r.standardized.mean_return_on_standardized_capital !== null
                    ? `${(Number(r.standardized.mean_return_on_standardized_capital) * 100).toFixed(1)}%`
                    : "—"}
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">Median Return on Standardized Capital</span>
                <span className="stat-value">
                  {r.standardized.median_return_on_standardized_capital !== null
                    ? `${(Number(r.standardized.median_return_on_standardized_capital) * 100).toFixed(1)}%`
                    : "—"}
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">Total Realized P&amp;L</span>
                <span className="stat-value">{money(r.standardized.total_realized_pnl)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Portfolio Drawdown</span>
                <span className="stat-value">
                  {r.standardized.portfolio_drawdown_available ? "Available" : "Not Available"}
                </span>
              </div>
            </div>
            <p className="text-sm text-faint" style={{ marginTop: 8, marginBottom: 0 }}>
              {r.standardized.portfolio_drawdown_reason}
            </p>
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
          {/* V4.1 methodology foundation (2026-08-31), Section 12 --
              read-side terminology fix only, no stored value changed.
              The underlying number is historical_compatibility.compatible_pct
              (analytics/options/move_compatibility.py) -- a historical base
              rate of this company's own past earnings moves against a
              strategy's breakeven distance, never a calibrated probability
              of profit. See services/decision_snapshot_freezing.py and
              the forensic audit's Part I Section 12. */}
          <h2>Historical Compatibility vs. Realized Outcome</h2>
          <table>
            <thead>
              <tr>
                <th>Historical Move Compatibility</th>
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
            Whether a company's own historical earnings-move frequency actually correlates with
            the real settlement outcome — buckets with very few decisions are not statistically
            meaningful yet.
          </p>
          <p className="text-sm text-faint" style={{ marginTop: 4, marginBottom: 0 }}>
            This is not a calibrated probability of profit. It is a backward-looking base rate:
            how often this company's own past earnings moves would have cleared a strategy's
            breakeven distance — never a forecast, and never adjusted for the real Wilson
            confidence interval also computed elsewhere for this same figure.
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
