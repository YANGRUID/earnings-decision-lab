import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { useAsync } from "../../hooks/useAsync";
import { CONFIG_ORDER, configLabel, statusPill } from "./shared";
import type { HealthState } from "../../types/api";

// V4 consolidation, Section 18 -- the Dashboard's professional-terminal
// header: TODAY status, V4 decisions, forward performance, readiness.
// Every value is read from a real endpoint; nothing is fabricated, and an
// unavailable value renders as "—".

function nowIn(tz: string) {
  return new Date().toLocaleTimeString("en-US", { timeZone: tz, hour: "2-digit", minute: "2-digit" });
}

function marketSession(): string {
  const et = new Date(new Date().toLocaleString("en-US", { timeZone: "America/New_York" }));
  const day = et.getDay();
  const mins = et.getHours() * 60 + et.getMinutes();
  if (day === 0 || day === 6) return "Closed (weekend)";
  if (mins < 9 * 60 + 30) return "Pre-market";
  if (mins < 16 * 60) return "Open";
  return "Closed";
}

function nextWindow(hour: number, minute: number): string {
  const et = new Date(new Date().toLocaleString("en-US", { timeZone: "America/New_York" }));
  const target = new Date(et);
  target.setHours(hour, minute, 0, 0);
  if (target <= et) target.setDate(target.getDate() + 1);
  while (target.getDay() === 0 || target.getDay() === 6) target.setDate(target.getDate() + 1);
  return target.toLocaleString("en-US", { weekday: "short", hour: "2-digit", minute: "2-digit" }) + " ET";
}

function light(state: HealthState | string | null | undefined): string {
  return state === "green" ? "pill pill-positive" : state === "yellow" ? "pill pill-warning" : state === "red" ? "pill pill-negative" : "pill pill-neutral";
}

export function DashboardV4Header() {
  const ops = useAsync(() => api.getOperationsSummary(), []);
  const prep = useAsync(() => api.getOperationsPreparationProgress(), []);
  const decisions = useAsync(() => api.getV4ShadowDecisions(), []);
  const record = useAsync(() => api.getV4TrackRecordByConfiguration(), []);
  const h = ops.data?.health;
  const v4Enabled = !!h?.v4_shadow?.enabled;
  const latest = decisions.data?.decisions.slice(0, 5) ?? [];
  const settled = record.data?.configurations.reduce((a, r) => a + (r.settled ?? 0), 0) ?? 0;

  return (
    <>
      <div className="card" data-testid="dashboard-today">
        <h2>Today</h2>
        <div className="grid grid-4" style={{ gap: 10 }}>
          <div className="stat"><span className="stat-label">New York</span><span className="stat-value mono">{nowIn("America/New_York")}</span><span className="text-faint text-sm">{marketSession()}</span></div>
          <div className="stat"><span className="stat-label">Zurich</span><span className="stat-value mono">{nowIn("Europe/Zurich")}</span></div>
          <div className="stat"><span className="stat-label">Market data</span>
            <span className="stat-value small">{h ? <span className={light(h.ibkr.state)}>{h.ibkr.provider.toUpperCase()} · {(h.ibkr.market_data_quality ?? "—").toUpperCase()}</span> : "—"}</span>
            {h && h.ibkr.connected && !h.ibkr.market_data_quality && (
              <span className="text-faint text-sm" data-testid="md-cold-start">Awaiting first market-data observation</span>
            )}
          </div>
          <div className="stat"><span className="stat-label">Next windows</span>
            <span className="stat-value small mono">Decision {nextWindow(15, 30)}</span>
            <span className="text-faint text-sm mono">Settlement 15:30 ET, T+1</span>
          </div>
        </div>
      </div>

      <div className="grid grid-2" style={{ gap: 12, alignItems: "start" }}>
        <div className="card" data-testid="dashboard-v4-decisions">
          <h2>V4 decisions <Link className="text-link text-sm" to="/v4-decision-lab">open lab →</Link></h2>
          {decisions.loading && !decisions.data ? <div className="text-muted">Loading…</div> : latest.length === 0 ? (
            <div className="empty-state">No V4 decisions yet. {v4Enabled ? "The next natural 15:30 ET run will appear here." : "The V4 forward test is disabled."}</div>
          ) : (
            <table>
              <thead><tr><th>Ticker</th><th>View</th><th>Status</th><th>Observed</th></tr></thead>
              <tbody>
                {latest.map((d) => {
                  const p = statusPill(d.status);
                  return (
                    <tr key={d.id}>
                      <td className="mono"><Link className="text-link" to={`/v4-decision-lab/${d.id}`}>{d.ticker}</Link></td>
                      <td>{(d.view?.direction ?? "—").toUpperCase()}</td>
                      <td><span className={p.className}>{p.label}</span></td>
                      <td className="mono text-sm">{new Date(d.legal_decision_window_at).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        <div className="card" data-testid="dashboard-readiness">
          <h2>System readiness</h2>
          {!h ? <div className="text-muted">Loading…</div> : (
            <table>
              <tbody>
                <tr><td>TWS</td><td><span className={light(h.ibkr.state)}>{h.ibkr.connected ? "connected" : "disconnected"}</span></td></tr>
                <tr><td>Calendar</td><td><span className={light(h.earnings_calendar.state)}>{h.earnings_calendar.active_provider ?? "—"}</span></td></tr>
                <tr><td>DeepSeek</td><td><span className={light(h.ai_provider.state)}>{h.ai_provider.configured ? "configured" : "not configured"}</span></td></tr>
                <tr><td>Research worker</td><td><span className={prep.data?.worker_active ? "pill pill-positive" : "pill pill-warning"}>{prep.data ? (prep.data.worker_active ? "active" : "idle") + ` · queue ${prep.data.queue_depth}` : "—"}</span></td></tr>
                <tr><td>Scheduler</td><td><span className={light(h.scheduler.state)}>{h.scheduler.registered_job_count} jobs</span></td></tr>
                <tr><td>Database</td><td><span className={light(h.database.state)}>{h.database.migration_head ?? "—"}</span></td></tr>
                <tr><td>V4 forward test</td><td><span className={v4Enabled ? "pill pill-positive" : "pill pill-neutral"}>{v4Enabled ? "active" : "Disabled"}</span></td></tr>
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="card" data-testid="dashboard-performance">
        <h2>Forward performance <Link className="text-link text-sm" to="/v4-shadow-track-record">track record →</Link></h2>
        {settled < 30 ? (
          <div className="notice"><strong>INSUFFICIENT SAMPLE</strong> — {settled} settled V4 observation{settled === 1 ? "" : "s"}. Performance metrics are withheld below 30 settled per cohort.</div>
        ) : null}
        {record.data && (
          <div className="grid grid-3" style={{ gap: 8, marginTop: 8 }}>
            {CONFIG_ORDER.map((k) => {
              const r = record.data!.configurations.find((c) => c.configuration_key === k);
              return (
                <div className="stat" key={k}>
                  <span className="stat-label">{configLabel(k)}</span>
                  <span className="stat-value small mono">{r ? `${r.actionable} act · ${r.no_action} no-action` : "—"}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </>
  );
}
