import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { MovePill } from "../components/MovePill";
import { EarningsCalendarGrid } from "../components/EarningsCalendarGrid";
import {
  deriveLifecycleStage,
  earningsCountdownLabel,
  LIFECYCLE_LABELS,
  TIMING_LABELS,
} from "../lib/decisionLifecycle";
import { formatMoney, formatPlainPercent } from "../lib/format";
import type {
  DecisionSnapshot,
  EarningsCalendarEvent,
  EntryCaptureAttempt,
  Rate,
  ResearchOverview,
  SettlementCaptureAttempt,
} from "../types/api";

// --------------------------------------------------------------------------
// Data loading -- a small, fixed number of bulk requests (never one
// request per calendar event/decision), matching this project's own
// personal-scale-project convention (see e.g. services/scheduler.py's
// own "small real decision count" reasoning on the backend).
// --------------------------------------------------------------------------

interface DashboardCore {
  events: EarningsCalendarEvent[];
  decisions: DecisionSnapshot[];
  capturedEntries: EntryCaptureAttempt[];
  capturedSettlements: SettlementCaptureAttempt[];
}

async function fetchDashboardCore(): Promise<DashboardCore> {
  const [events, decisions, capturedEntries, capturedSettlements] = await Promise.all([
    api.listUpcomingEarnings({ limit: 100 }),
    api.listDecisionSnapshots({ limit: 200 }),
    api.listBenchmarkEntries({ status: "captured", limit: 200 }),
    api.listAllSettlements({ status: "captured", limit: 200 }),
  ]);
  return { events, decisions, capturedEntries, capturedSettlements };
}

/** Best-effort, supplementary market context (implied move / historical
 * average move) -- the same real, objective facts the existing Search
 * page already shows (src/pages/Dashboard.tsx), sourced per-ticker since
 * no bulk endpoint for this V3 concept exists. A slow or failed lookup
 * for one ticker never blocks the rest of the page -- allSettled, not
 * all. */
function useMarketContext(tickers: string[]) {
  const key = tickers.join(",");
  return useAsync(async () => {
    const results = await Promise.allSettled(tickers.map((t) => api.getResearchOverview(t)));
    const byTicker: Record<string, ResearchOverview> = {};
    results.forEach((result, i) => {
      if (result.status === "fulfilled") byTicker[tickers[i]] = result.value;
    });
    return byTicker;
  }, [key]);
}

// --------------------------------------------------------------------------
// Section A -- Upcoming Earnings
// --------------------------------------------------------------------------

function decisionStatusBadge(stage: ReturnType<typeof deriveLifecycleStage> | null) {
  if (stage === null) {
    return <span className="pill pill-neutral">No AI decision generated yet</span>;
  }
  const cls = stage === "settled" ? "pill-positive" : stage === "entered" ? "pill-neutral" : "pill-neutral";
  return <span className={`pill ${cls}`}>{LIFECYCLE_LABELS[stage]}</span>;
}

function UpcomingEarningsCard({
  event,
  decision,
  entries,
  settlements,
  overview,
}: {
  event: EarningsCalendarEvent;
  decision: DecisionSnapshot | undefined;
  entries: EntryCaptureAttempt[];
  settlements: SettlementCaptureAttempt[];
  overview: ResearchOverview | undefined;
}) {
  const stage = decision ? deriveLifecycleStage(entries, settlements) : null;
  const impliedMove = overview?.latest_volatility_snapshot?.implied_move_pct ?? null;
  const historicalMove = overview?.historical_moves?.average_abs_move_pct ?? null;

  return (
    <Link
      to={`/earnings-calendar/${event.symbol}`}
      className="card ticker-card"
      style={{ display: "block" }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <div className="ticker-card-symbol">{event.symbol}</div>
          <div className="ticker-card-name">{event.company_name}</div>
        </div>
        <span className="pill pill-neutral">{earningsCountdownLabel(event.earnings_date)}</span>
      </div>
      <div className="grid grid-3" style={{ gap: 8, marginTop: 10 }}>
        <div className="stat">
          <span className="stat-label">Earnings date</span>
          <span className="stat-value small">
            {new Date(`${event.earnings_date}T00:00:00`).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
            })}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Timing</span>
          <span className="stat-value small">{TIMING_LABELS[event.earnings_time]}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Implied move</span>
          <span className="stat-value small">
            {impliedMove !== null ? <MovePill value={impliedMove} /> : "Not available"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Historical avg move</span>
          <span className="stat-value small">
            {historicalMove !== null ? formatPlainPercent(historicalMove) : "No history yet"}
          </span>
        </div>
        <div className="stat" style={{ gridColumn: "span 2" }}>
          <span className="stat-label">AI decision status</span>
          <span className="stat-value small">{decisionStatusBadge(stage)}</span>
        </div>
      </div>
    </Link>
  );
}

function UpcomingEarningsSection({ core }: { core: DashboardCore }) {
  const tickers = core.events.map((e) => e.symbol);
  const context = useMarketContext(tickers);

  const decisionByEventId = new Map<number, DecisionSnapshot>();
  for (const d of core.decisions) {
    if (!decisionByEventId.has(d.earnings_calendar_event_id)) {
      decisionByEventId.set(d.earnings_calendar_event_id, d);
    }
  }
  const entriesByDecisionId = new Map<number, EntryCaptureAttempt[]>();
  for (const e of core.capturedEntries) {
    const list = entriesByDecisionId.get(e.decision_snapshot_id) ?? [];
    list.push(e);
    entriesByDecisionId.set(e.decision_snapshot_id, list);
  }
  const settlementsByDecisionId = new Map<number, SettlementCaptureAttempt[]>();
  for (const s of core.capturedSettlements) {
    const list = settlementsByDecisionId.get(s.decision_snapshot_id) ?? [];
    list.push(s);
    settlementsByDecisionId.set(s.decision_snapshot_id, list);
  }

  if (core.events.length === 0) {
    return (
      <div className="card">
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          No upcoming earnings on the calendar yet — the daily Finnhub sync populates this once
          it has run (services/scheduler.py::run_earnings_calendar_sync_job).
        </p>
      </div>
    );
  }

  return (
    <div className="grid grid-3">
      {core.events.map((event) => {
        const decision = decisionByEventId.get(event.id);
        return (
          <UpcomingEarningsCard
            key={event.id}
            event={event}
            decision={decision}
            entries={decision ? (entriesByDecisionId.get(decision.id) ?? []) : []}
            settlements={decision ? (settlementsByDecisionId.get(decision.id) ?? []) : []}
            overview={context.data?.[event.symbol]}
          />
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------
// Section B -- AI Decisions
// --------------------------------------------------------------------------

function riskRewardCell(entry: EntryCaptureAttempt | undefined) {
  if (!entry || entry.status !== "captured") {
    return <span className="text-faint">Awaiting entry</span>;
  }
  return (
    <span className="mono">
      Risk {entry.initial_max_risk ? `$${formatMoney(entry.initial_max_risk)}` : "—"}
      {" · "}
      {entry.capital_utilization ? `${formatMoney(entry.capital_utilization, 1)}%` : "—"} of
      capital
    </span>
  );
}

function AiDecisionsSection({ core }: { core: DashboardCore }) {
  const researched = core.decisions.filter((d) => d.strategy_type !== null && d.legs !== null);
  const entriesByDecisionId = new Map<number, EntryCaptureAttempt[]>();
  for (const e of core.capturedEntries) {
    const list = entriesByDecisionId.get(e.decision_snapshot_id) ?? [];
    list.push(e);
    entriesByDecisionId.set(e.decision_snapshot_id, list);
  }
  const settlementsByDecisionId = new Map<number, SettlementCaptureAttempt[]>();
  for (const s of core.capturedSettlements) {
    const list = settlementsByDecisionId.get(s.decision_snapshot_id) ?? [];
    list.push(s);
    settlementsByDecisionId.set(s.decision_snapshot_id, list);
  }

  if (researched.length === 0) {
    return (
      <div className="card">
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          No AI decision generated yet.
        </p>
      </div>
    );
  }

  return (
    <div className="card">
      <table>
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Strategy</th>
            <th>Direction</th>
            <th>Confidence</th>
            <th>Risk / Reward</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {researched.map((d) => {
            const entries = entriesByDecisionId.get(d.id) ?? [];
            const settlements = settlementsByDecisionId.get(d.id) ?? [];
            const stage = deriveLifecycleStage(entries, settlements);
            const capturedEntry = entries.find((e) => e.status === "captured");
            return (
              <tr key={d.id}>
                <td className="mono">
                  <Link to={`/earnings-calendar/${d.ticker}`} className="text-link">
                    {d.ticker}
                  </Link>
                </td>
                <td className="mono">{d.strategy_type}</td>
                <td className="mono">{d.strategy_direction}</td>
                <td className="mono">
                  {d.estimated_probability ? formatPlainPercent(d.estimated_probability, 0) : "—"}
                </td>
                <td>{riskRewardCell(capturedEntry)}</td>
                <td>
                  <span
                    className={`pill ${stage === "settled" ? "pill-positive" : "pill-neutral"}`}
                  >
                    {LIFECYCLE_LABELS[stage]}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------
// Section C -- Performance Summary (reuses the existing Track Record API,
// Phase 4.6)
// --------------------------------------------------------------------------

function pct(rate: Rate): string {
  if (rate.pct === null) return "—";
  return `${(Number(rate.pct) * 100).toFixed(0)}%`;
}

function PerformanceSummarySection() {
  const record = useAsync(() => api.getBenchmarkTrackRecord(), []);

  if (record.loading && !record.data) {
    return <LoadingState label="Loading performance summary…" />;
  }
  if (record.error && !record.data) return <ErrorState message={record.error} />;
  if (!record.data) return null;

  const r = record.data;

  if (r.settled_decisions === 0) {
    return (
      <div className="card">
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          No settled trades available.
        </p>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="grid grid-3" style={{ gap: 16 }}>
        <div className="stat">
          <span className="stat-label">Win Rate</span>
          <span className="stat-value">{pct(r.win_rate)}</span>
          <span className="text-sm text-faint">N = {r.win_rate.total}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Average R</span>
          <span className="stat-value">{r.average_r !== null ? Number(r.average_r).toFixed(2) : "—"}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Profit Factor</span>
          <span className="stat-value">
            {r.profit_factor !== null ? Number(r.profit_factor).toFixed(2) : "—"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Max Drawdown</span>
          <span className="stat-value">
            {r.max_drawdown !== null ? `$${formatMoney(r.max_drawdown)}` : "—"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Settled Decisions</span>
          <span className="stat-value">{r.settled_decisions}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Total Decisions</span>
          <span className="stat-value">{r.total_decisions}</span>
        </div>
      </div>
      <p className="text-sm text-faint" style={{ marginTop: 10, marginBottom: 0 }}>
        <Link to="/benchmark-track-record" className="text-link">
          Full breakdown, prediction accuracy, and probability calibration →
        </Link>
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------

export function EarningsAnalystDashboard() {
  const core = useAsync(fetchDashboardCore, []);

  if (core.loading && !core.data) return <LoadingState label="Loading dashboard…" />;
  if (core.error && !core.data) return <ErrorState message={core.error} />;
  if (!core.data) return null;

  return (
    <div>
      <div className="page-header">
        <h1>AI Earnings Analyst Dashboard</h1>
        <p>
          Real, verified forward-test data — upcoming earnings, what the AI actually decided, and
          how the real $2,000 benchmark portfolio has performed. Nothing here is estimated or
          back-filled.
        </p>
      </div>

      <h2 style={{ fontSize: 15, textTransform: "uppercase", letterSpacing: "0.03em" }}>
        Earnings Calendar
      </h2>
      <div style={{ marginBottom: 24 }}>
        <EarningsCalendarGrid />
      </div>

      <h2 style={{ fontSize: 15, textTransform: "uppercase", letterSpacing: "0.03em" }}>
        Upcoming Earnings
      </h2>
      <UpcomingEarningsSection core={core.data} />

      <h2 style={{ fontSize: 15, textTransform: "uppercase", letterSpacing: "0.03em", marginTop: 24 }}>
        AI Decisions
      </h2>
      <AiDecisionsSection core={core.data} />

      <h2 style={{ fontSize: 15, textTransform: "uppercase", letterSpacing: "0.03em", marginTop: 24 }}>
        Performance Summary
      </h2>
      <PerformanceSummarySection />
    </div>
  );
}
