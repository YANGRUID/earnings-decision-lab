import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { ListToolbar, Pager } from "../components/ListControls";
import { useListControls } from "../hooks/useListControls";
import { EarningsCalendarGrid } from "../components/EarningsCalendarGrid";
import { DashboardV4Header } from "../components/v4/DashboardV4Header";
import { TickerSearchBar } from "../components/TickerSearchBar";
import { fmtMarketCap } from "../components/v4/shared";
import { countdown, formatDateTime, stateLabel } from "../lib/operationsFormat";
import type { PipelineEvent } from "../types/api";

// Dashboard -- V4-only reset (2026-09-02). The header reads today's window,
// the latest V4 decisions, readiness and the six-cohort forward record. The
// pipeline below is the same backend state machine Live Operations shows,
// read in ONE bulk request (never one request per ticker), so the page can
// never hold the browser's connection budget after a navigation.

const TIMING_SHORT: Record<string, string> = { bmo: "BMO", amc: "AMC", dmh: "DMH", unknown: "?" };

const PILL: Record<string, string> = {
  RESEARCH_READY: "positive",
  ENTRY_OBSERVED: "positive",
  SETTLED: "positive",
  CALENDAR_DISCOVERED: "neutral",
  BUSINESS_INELIGIBLE: "neutral",
  NO_ACTION: "neutral",
};

function pillFor(state: string): string {
  if (PILL[state]) return PILL[state];
  return /FAILED|MISSED|SKIPPED/.test(state) ? "negative" : "warning";
}

function UpcomingPipelineSection() {
  const events = useAsync((signal) => api.getOperationsEvents({ signal }), []);
  const rows = (events.data?.events ?? []).filter((e) => e.lifecycle_state !== "BUSINESS_INELIGIBLE");
  const controls = useListControls<PipelineEvent>({
    rows,
    urlKey: "dash",
    searchKeys: [(e) => e.symbol, (e) => e.company_name, (e) => e.lifecycle_reason],
    facet: { label: "State", getValue: (e) => e.lifecycle_state, format: stateLabel },
    sorts: [
      { key: "earnings", label: "Earnings date", compare: (a, b) => a.earnings_date.localeCompare(b.earnings_date) || a.symbol.localeCompare(b.symbol) },
      { key: "cap", label: "Market cap (largest first)", compare: (a, b) => Number(b.market_cap ?? -Infinity) - Number(a.market_cap ?? -Infinity) },
      { key: "ticker", label: "Ticker (A–Z)", compare: (a, b) => a.symbol.localeCompare(b.symbol) },
    ],
    defaultSort: "earnings",
    defaultPageSize: 25,
  });
  if (events.loading && !events.data) return <LoadingState label="Loading the V4 pipeline…" />;
  if (events.error && !events.data) return <ErrorState message={events.error} />;
  const now = new Date().toISOString();
  return (
    <div className="card" data-testid="dashboard-pipeline">
      <h2 style={{ marginTop: 0 }}>Upcoming Earnings — V4 pipeline</h2>
      {rows.length === 0 ? (
        <p className="text-sm text-muted" style={{ margin: 0 }}>No eligible earnings events in the pipeline window (2 days back, 7 days ahead).</p>
      ) : (
        <>
          <ListToolbar controls={controls} placeholder="Search ticker or company" testId="dashboard-pipeline-controls" />
          <table>
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Company</th>
                <th>Market cap</th>
                <th>Earnings</th>
                <th>Decision (ET)</th>
                <th>Settlement (ET)</th>
                <th>State</th>
                <th>Next</th>
              </tr>
            </thead>
            <tbody>
              {controls.visible.map((e) => (
                <tr key={e.calendar_event_id} data-state={e.lifecycle_state}>
                  <td className="mono"><Link to={`/company/${e.symbol}`}>{e.symbol}</Link></td>
                  <td>{e.company_name}</td>
                  <td className="mono">{fmtMarketCap(e.market_cap)}</td>
                  <td className="mono">{e.earnings_date} {TIMING_SHORT[e.earnings_timing] ?? e.earnings_timing}</td>
                  <td className="mono text-sm">{formatDateTime(e.entry_timestamp)}</td>
                  <td className="mono text-sm">{formatDateTime(e.exit_timestamp)}</td>
                  <td>
                    <span className={`pill pill-${pillFor(e.lifecycle_state)}`}>{stateLabel(e.lifecycle_state)}</span>
                    {e.shadow_decision_id && (
                      <>
                        {" "}
                        <Link className="text-link text-sm" to={`/v4-decision-lab/${e.shadow_decision_id}`}>decision →</Link>
                      </>
                    )}
                  </td>
                  <td className="text-sm text-muted">
                    {e.next_action ?? "—"}
                    {e.next_action_at && <span className="mono"> · {countdown(e.next_action_at, now)}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager controls={controls} testId="dashboard-pipeline-pager" />
        </>
      )}
    </div>
  );
}

export function EarningsAnalystDashboard() {
  return (
    <div>
      <div className="page-header">
        <h1>Dashboard</h1>
        <p>
          Real, prospective V4 forward-test evidence: the 15:30 ET decision, the observed entry and the
          15:30 ET settlement on the first post-earnings trading day. Nothing here is estimated,
          back-filled or executed as an order.
        </p>
      </div>
      <DashboardV4Header />
      <TickerSearchBar />
      <UpcomingPipelineSection />
      <h2 style={{ fontSize: 15, textTransform: "uppercase", letterSpacing: "0.03em" }}>Earnings Calendar</h2>
      <div style={{ marginBottom: 24 }}>
        <EarningsCalendarGrid />
      </div>
    </div>
  );
}
