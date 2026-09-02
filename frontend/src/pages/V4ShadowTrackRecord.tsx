import { useState } from "react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { CONFIG_ORDER, configLabel } from "../components/v4/shared";
import { ForwardTestNotice } from "../components/v4/sharedComponents";

// V4 Forward Track Record -- the primary performance page (Sections 28-31).
// Six cohorts, counts only until a cohort clears the sample floor. No
// portfolio drawdown, no Sharpe: there is no real capital ledger yet and
// No static pseudo-portfolio accounting is reproduced.
export function V4ShadowTrackRecord() {
  const [selected, setSelected] = useState<"all" | string>("all");
  const record = useAsync(() => api.getV4TrackRecordByConfiguration(), []);
  const overall = useAsync(() => api.getV4ShadowTrackRecord(), []);

  if (record.loading && !record.data) return <LoadingState label="Loading V4 forward track record…" />;
  if (record.error && !record.data) return <ErrorState message={record.error} />;
  if (!record.data) return null;
  const rows = record.data.configurations;
  const visible = selected === "all" ? rows : rows.filter((r) => r.configuration_key === selected);
  const totalEvents = rows.reduce((a, r) => Math.max(a, r.events), 0);
  const settledAll = rows.reduce((a, r) => Math.max(a, r.settled), 0);

  return (
    <div>
      <div className="page-header"><h1>V4 Forward Track Record</h1></div>
      <ForwardTestNotice text={record.data.notice} />

      <div className="card">
        <div className="tab-bar" data-testid="cohort-selector">
          <button className={`tab-button ${selected === "all" ? "active" : ""}`} onClick={() => setSelected("all")}>All configurations</button>
          {CONFIG_ORDER.map((k) => (
            <button key={k} className={`tab-button ${selected === k ? "active" : ""}`} onClick={() => setSelected(k)}>{configLabel(k)}</button>
          ))}
        </div>
      </div>

      {settledAll < record.data.sample_floor && (
        <div className="notice notice-critical" data-testid="insufficient-sample">
          <strong>INSUFFICIENT SAMPLE</strong> — {settledAll} settled V4 observation{settledAll === 1 ? "" : "s"} against a floor of {record.data.sample_floor}.
          Win rate, standardized returns and realized P&amp;L are withheld until a cohort clears the floor. Nothing here implies statistical confidence.
        </div>
      )}

      <div className="card">
        <h2>{selected === "all" ? "Six cohorts" : configLabel(selected)}</h2>
        {totalEvents === 0 ? (
          <div className="empty-state">
            <strong>No V4 forward observations yet.</strong> The cohort begins with the first natural 15:30 ET shadow run after activation. No history is back-filled and no sample rows are simulated.
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ fontVariantNumeric: "tabular-nums" }}>
              <thead>
                <tr>
                  <th>Configuration</th>
                  <th style={{ textAlign: "right" }}>Events</th>
                  <th style={{ textAlign: "right" }}>Actionable</th>
                  <th style={{ textAlign: "right" }}>No action</th>
                  <th style={{ textAlign: "right" }}>Failed</th>
                  <th style={{ textAlign: "right" }}>Entry observed</th>
                  <th style={{ textAlign: "right" }}>Settled</th>
                  <th>Sample</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((r) => (
                  <tr key={r.configuration_key}>
                    <td><strong>{configLabel(r.configuration_key)}</strong></td>
                    <td className="mono" style={{ textAlign: "right" }}>{r.events}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{r.actionable}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{r.no_action}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{r.failed}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{r.entry_observed}{r.entry_failed ? <span className="text-faint"> / {r.entry_failed} failed</span> : null}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{r.settled}{r.settlement_failed ? <span className="text-faint"> / {r.settlement_failed} failed</span> : null}</td>
                    <td><span className="pill pill-warning">{r.sample_sufficiency}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-faint text-sm" style={{ marginTop: 8 }}>{record.data.metrics_note}</p>
      </div>

      {overall.data && (
        <div className="card">
          <h2>Event-level shadow counts</h2>
          <div className="grid grid-4" style={{ gap: 10 }}>
            {(
              [
                ["Shadow decisions", overall.data.counts.shadow_decisions],
                ["Ranked", overall.data.counts.ranked],
                ["No action", overall.data.counts.no_action],
                ["Entry observed", overall.data.counts.entry_observed],
                ["Required quote unavailable", overall.data.counts.entry_not_executable],
                ["Settled", overall.data.counts.settled],
                ["Settlement observation error", overall.data.counts.settlement_failed],
                ["Failed", overall.data.counts.failed],
              ] as [string, number][]
            ).map(([label, n]) => (
              <div className="stat" key={label}>
                <span className="stat-label">{label}</span>
                <span className="stat-value mono">{n}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
