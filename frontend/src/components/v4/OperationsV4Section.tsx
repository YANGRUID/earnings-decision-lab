import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { useAsync } from "../../hooks/useAsync";
import { CONFIG_ORDER, configLabel, statusPill } from "./shared";

// V4 consolidation, Sections 36-39 -- the EXPERIMENTAL FORWARD (V4)
// domain of Operations, kept separate from CONTROL / OFFICIAL (V3). One
// row per V4 event, expandable into its six configurations; counters are
// never mixed with V3's.

function V4EventRow({ id, ticker, status, when }: { id: number; ticker: string; status: string; when: string }) {
  const [open, setOpen] = useState(false);
  const cfgs = useAsync(() => (open ? api.getV4ShadowConfigurations(id) : Promise.resolve(null)), [open, id]);
  const p = statusPill(status);
  return (
    <>
      <tr onClick={() => setOpen((o) => !o)} style={{ cursor: "pointer" }}>
        <td className="mono">{open ? "▾" : "▸"} <Link className="text-link" to={`/v4-decision-lab/${id}`} onClick={(e) => e.stopPropagation()}>{ticker}</Link></td>
        <td><span className={p.className}>{p.label}</span></td>
        <td className="mono text-sm">{new Date(when).toLocaleString("en-US", { timeZone: "America/New_York" })} ET</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={3}>
            {cfgs.loading && !cfgs.data ? <span className="text-muted">Loading configurations…</span> : (
              <table style={{ fontSize: ".85rem" }}>
                <tbody>
                  {CONFIG_ORDER.map((k) => {
                    const c = cfgs.data?.configurations.find((x) => x.configuration_key === k);
                    if (!c) return null;
                    const cp = statusPill(c.status);
                    return (
                      <tr key={k}>
                        <td>{configLabel(k)}</td>
                        <td><span className={cp.className}>{cp.label}</span></td>
                        <td className="text-muted">{c.status === "RANKED" ? c.rank_1_candidate_id : c.no_action_reason ?? ""}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export function OperationsV4Section({ registeredJobCount }: { registeredJobCount: number | null }) {
  const decisions = useAsync(() => api.getV4ShadowDecisions(), []);
  const enabled = (registeredJobCount ?? 0) > 5;
  const rows = decisions.data?.decisions.slice(0, 20) ?? [];
  return (
    <div className="card" data-testid="operations-v4">
      <h2>Experimental Forward Test — V4 <span className="pill pill-neutral">shadow</span></h2>
      <div className="grid grid-4" style={{ gap: 10 }}>
        <div className="stat"><span className="stat-label">V4 decision window</span><span className="stat-value small mono">15:30 ET</span><span className="text-faint text-sm mono">v4-pre-earnings-1530et-v1</span></div>
        <div className="stat"><span className="stat-label">V4 settlement window</span><span className="stat-value small mono">15:55 ET</span><span className="text-faint text-sm">unchanged from V3</span></div>
        <div className="stat"><span className="stat-label">Shadow scheduler</span>
          <span className="stat-value small">{enabled ? <span className="pill pill-positive">active</span> : <span className="pill pill-neutral">Disabled — awaiting live activation gate</span>}</span>
        </div>
        <div className="stat"><span className="stat-label">Shadow decisions</span><span className="stat-value mono">{decisions.data?.decisions.length ?? "—"}</span></div>
      </div>
      {rows.length === 0 ? (
        <div className="empty-state" style={{ marginTop: 10 }}>No V4 shadow decisions have been frozen. Nothing is back-filled.</div>
      ) : (
        <table style={{ marginTop: 10 }}>
          <thead><tr><th>Event</th><th>Status</th><th>Observed</th></tr></thead>
          <tbody>
            {rows.map((d) => <V4EventRow key={d.id} id={d.id} ticker={d.ticker} status={d.status} when={d.legal_decision_window_at} />)}
          </tbody>
        </table>
      )}
    </div>
  );
}
