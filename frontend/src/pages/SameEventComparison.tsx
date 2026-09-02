import { useParams, useNavigate, Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { EmptyState, ErrorState, LoadingState } from "../components/StatusStates";
import { humanStatus, humanStrategy, money, pct, statusPill } from "../components/v4/shared";
import { ExperimentalNotice } from "../components/v4/sharedComponents";

// Same-Event Comparison (Sections 33-35): V3 control vs six V4 configurations
// for ONE earnings event. V3 and V4 numbers stay in separate panels; the
// 15:55 vs 15:30 timing difference is stated, never hidden; no headline
// claims one engine beats the other.
export function SameEventComparison() {
  const params = useParams<{ eventId?: string }>();
  const navigate = useNavigate();
  const eventId = params.eventId ? Number(params.eventId) : null;
  const decisions = useAsync(() => api.getV4ShadowDecisions(), []);
  const cmp = useAsync(() => (eventId ? api.getSameEventComparison(eventId) : Promise.resolve(null)), [eventId]);

  if (!eventId) {
    const rows = decisions.data?.decisions ?? [];
    return (
      <div>
        <div className="page-header"><h1>Same-Event Comparison</h1></div>
        <ExperimentalNotice />
        <div className="card">
          <h2>Choose an earnings event</h2>
          {decisions.loading && !decisions.data ? <LoadingState /> : rows.length === 0 ? (
            <EmptyState><strong>No V4 decisions to compare yet.</strong> A comparison needs at least one V4 shadow decision for an event. None exist until the first natural shadow run after activation.</EmptyState>
          ) : (
            <table>
              <thead><tr><th>Ticker</th><th>Company</th><th>Observed (ET)</th><th></th></tr></thead>
              <tbody>
                {rows.map((d) => (
                  <tr key={d.id}>
                    <td className="mono"><strong>{d.ticker}</strong></td>
                    <td>{d.company_name}</td>
                    <td className="mono">{new Date(d.legal_decision_window_at).toLocaleString("en-US", { timeZone: "America/New_York" })}</td>
                    <td><button className="btn-secondary" onClick={() => navigate(`/same-event-comparison/${d.earnings_calendar_event_id}`)}>Compare</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    );
  }
  if (cmp.loading && !cmp.data) return <LoadingState label="Loading comparison…" />;
  if (cmp.error && !cmp.data) return <ErrorState message={cmp.error} />;
  const data = cmp.data;
  if (!data) return null;
  const v3 = data.v3_control, v4 = data.v4_shadow;

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1 style={{ margin: 0 }}><span className="mono">{data.event.symbol}</span> — {data.event.company_name}</h1>
        <Link className="btn-secondary" to="/same-event-comparison">← All events</Link>
      </div>
      <ExperimentalNotice text={data.notice} />
      <div className="notice" data-testid="timing-note"><strong>Different clocks.</strong> {data.timing_note}</div>

      <div className="grid grid-2" style={{ gap: 12, alignItems: "start" }}>
        <div className="card" data-testid="v3-panel">
          <h2>V3 Historical Control <span className="pill pill-neutral">legacy methodology</span></h2>
          {!v3 ? <EmptyState>No official V3 decision exists for this event.</EmptyState> : (
            <>
              <div className="grid grid-2" style={{ gap: 8 }}>
                <div className="stat"><span className="stat-label">Observation</span><span className="stat-value small mono">{v3.observation_time_et} ET</span><span className="text-faint text-sm mono">{v3.timing_policy_version}</span></div>
                <div className="stat"><span className="stat-label">Strategy</span><span className="stat-value small">{humanStrategy(v3.strategy)}</span><span className="text-faint text-sm">{v3.direction ?? ""} · {v3.risk_profile ?? ""}</span></div>
                <div className="stat"><span className="stat-label">Underlying</span><span className="stat-value small mono">{money(v3.underlying_price, 2)}</span></div>
                <div className="stat"><span className="stat-label">Entry</span>
                  <span className="stat-value small">{v3.entry ? <span className={statusPill(v3.entry.status).className}>{humanStatus(v3.entry.status)}</span> : "—"}</span>
                  <span className="text-faint text-sm">{v3.entry?.capture_error ?? (v3.entry ? `${v3.entry.contracts ?? 0} contract(s), risk ${money(v3.entry.initial_max_risk)}` : "")}</span>
                </div>
                <div className="stat"><span className="stat-label">T+1 result</span>
                  <span className="stat-value small mono">{v3.settlement ? money(v3.settlement.realized_pnl, 2) : "Waiting for post-earnings settlement observation"}</span>
                </div>
              </div>
              <p className="text-faint text-sm">V3 metrics use V3's own methodology and are not comparable one-for-one with V4's modeled T+1 economics.</p>
            </>
          )}
        </div>

        <div className="card" data-testid="v4-panel">
          <h2>V4 Experimental Shadow <span className="pill pill-neutral">six configurations</span></h2>
          {!v4 ? <EmptyState>No V4 shadow decision exists for this event.</EmptyState> : (
            <>
              <div className="grid grid-2" style={{ gap: 8 }}>
                <div className="stat"><span className="stat-label">Observation</span><span className="stat-value small mono">{v4.observation_time_et} ET</span><span className="text-faint text-sm mono">{v4.timing_policy_version}</span></div>
                <div className="stat"><span className="stat-label">Underlying</span><span className="stat-value small mono">{money(v4.underlying_price, 2)}</span><span className="text-faint text-sm">{(v4.market_data_quality ?? "").toUpperCase()}</span></div>
                <div className="stat"><span className="stat-label">Entry observation</span><span className="stat-value small">{v4.entry_observation ? <span className={statusPill(v4.entry_observation.status).className}>{humanStatus(v4.entry_observation.status)}</span> : "—"}</span></div>
                <div className="stat"><span className="stat-label">T+1 result</span><span className="stat-value small mono">{v4.settlement ? pct(v4.settlement.return_on_standardized_capital) : "Waiting for post-earnings settlement observation"}</span></div>
              </div>
              <table style={{ marginTop: 10, fontVariantNumeric: "tabular-nums" }}>
                <thead><tr><th>Configuration</th><th>Action</th><th>Strategy</th><th style={{ textAlign: "right" }}>Capital</th><th style={{ textAlign: "right" }}>Max risk</th><th style={{ textAlign: "right" }}>Core median</th><th style={{ textAlign: "right" }}>Core worst</th></tr></thead>
                <tbody>
                  {v4.configurations.map((c) => {
                    const pill = statusPill(c.status);
                    return (
                      <tr key={c.configuration_key}>
                        <td><strong>{c.label}</strong></td>
                        <td><span className={pill.className}>{pill.label}</span></td>
                        <td>{c.strategy ? humanStrategy(c.strategy) : <span className="text-faint" title={c.no_action_reason ?? ""}>—</span>}</td>
                        <td className="mono" style={{ textAlign: "right" }}>{money(c.entry_cash_required)}</td>
                        <td className="mono" style={{ textAlign: "right" }}>{money(c.max_risk_dollars)}</td>
                        <td className="mono" style={{ textAlign: "right" }}>{pct(c.core_median_return)}</td>
                        <td className="mono" style={{ textAlign: "right" }}>{pct(c.core_worst_return)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </>
          )}
        </div>
      </div>
      <p className="text-faint text-sm">Raw evidence only. No engine is declared superior until a meaningful forward sample exists.</p>
    </div>
  );
}
