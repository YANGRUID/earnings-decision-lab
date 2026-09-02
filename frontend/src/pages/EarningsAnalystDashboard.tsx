import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { ListToolbar, Pager } from "../components/ListControls";
import { useListControls } from "../hooks/useListControls";
import { MovePill } from "../components/MovePill";
import { EarningsCalendarGrid } from "../components/EarningsCalendarGrid";
import { DashboardV4Header } from "../components/v4/DashboardV4Header";
import { HistoricalCompatibilityValue } from "../components/HistoricalCompatibility";
import { TickerSearchBar } from "../components/TickerSearchBar";
import { HISTORICAL_MOVE_COMPATIBILITY_EXPLANATION } from "../lib/historicalCompatibility";
import {
  deriveLifecycleStage,
  earningsCountdownLabel,
  LIFECYCLE_LABELS,
  LIFECYCLE_PILL_CLASS,
  TIMING_LABELS,
} from "../lib/decisionLifecycle";
import { formatMoney, formatPlainPercent, formatRelativeTime, providerLabel } from "../lib/format";
import type {
  DecisionSnapshot,
  DomainStatus,
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
  // Post-live correction (2026-08-25): every real entry attempt, not
  // just captured ones -- a status="captured" filter here silently made
  // every real FAILED attempt invisible to deriveLifecycleStage, which
  // is exactly why the Dashboard showed "Pending Entry" for Aug 25's
  // real entry failures instead of "Entry Failed".
  entries: EntryCaptureAttempt[];
  capturedSettlements: SettlementCaptureAttempt[];
  earningsCalendarStatus: DomainStatus | null;
}

async function fetchDashboardCore(): Promise<DashboardCore> {
  const [events, decisions, entries, capturedSettlements, systemStatus] = await Promise.all([
    api.listUpcomingEarnings({ limit: 100 }),
    api.listDecisionSnapshots({ limit: 200 }),
    api.listBenchmarkEntries({ limit: 200 }),
    api.listAllSettlements({ status: "captured", limit: 200 }),
    api.getSystemStatus(),
  ]);
  const earningsCalendarStatus =
    systemStatus.providers.domains.find((d) => d.domain === "earnings_calendar") ?? null;
  return { events, decisions, entries, capturedSettlements, earningsCalendarStatus };
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
  return (
    <span className={`pill ${LIFECYCLE_PILL_CLASS[stage]}`}>{LIFECYCLE_LABELS[stage]}</span>
  );
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
  const stage = decision ? deriveLifecycleStage(entries, settlements, decision) : null;
  const impliedMove = overview?.latest_volatility_snapshot?.implied_move_pct ?? null;
  const historicalMove = overview?.historical_moves?.average_abs_move_pct ?? null;
  // GET /research/{symbol}/overview always returns 200 (company: null
  // for a symbol nobody has researched yet, never a 404 -- see api/
  // routers/research.py::research_overview) -- overview.company is the
  // real signal for "has this ticker already been searched," not
  // whether the overview call itself succeeded. Already-researched ->
  // straight to that company's real workspace; otherwise -> Search,
  // pre-filled, so preparing it is one click away rather than a re-typed
  // ticker.
  const destination = overview?.company
    ? `/company/${event.symbol}`
    : `/search?ticker=${event.symbol}`;

  return (
    <Link to={destination} className="card ticker-card" style={{ display: "block" }}>
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
  // Long-list controls (2026-09-02): the card grid grows with the calendar
  // window; 12 cards per page, largest market cap first, everything
  // searchable and "All" one click away (?up_*). Market context is fetched
  // only for the cards on screen.
  const cards = useListControls({
    rows: core.events,
    urlKey: "up",
    searchKeys: [(e) => e.symbol, (e) => e.company_name],
    sorts: [
      {
        key: "cap",
        label: "Market cap (largest first)",
        compare: (a, b) => {
          const capA = a.market_cap !== null ? Number(a.market_cap) : -Infinity;
          const capB = b.market_cap !== null ? Number(b.market_cap) : -Infinity;
          return capB - capA;
        },
      },
      {
        key: "date",
        label: "Earnings date (soonest first)",
        compare: (a, b) => a.earnings_date.localeCompare(b.earnings_date) || a.symbol.localeCompare(b.symbol),
      },
      { key: "ticker", label: "Ticker (A–Z)", compare: (a, b) => a.symbol.localeCompare(b.symbol) },
    ],
    defaultSort: "cap",
    defaultPageSize: 12,
    pageSizes: [12, 24, 48],
  });
  const tickers = cards.visible.map((e) => e.symbol);
  const context = useMarketContext(tickers);

  const decisionByEventId = new Map<number, DecisionSnapshot>();
  for (const d of core.decisions) {
    if (!decisionByEventId.has(d.earnings_calendar_event_id)) {
      decisionByEventId.set(d.earnings_calendar_event_id, d);
    }
  }
  const entriesByDecisionId = new Map<number, EntryCaptureAttempt[]>();
  for (const e of core.entries) {
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
    const activeProvider =
      core.earningsCalendarStatus?.providers.find(
        (p) => p.provider === core.earningsCalendarStatus?.primary
      ) ?? null;
    const lastSync = activeProvider?.last_success_at ?? null;
    return (
      <div className="card">
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          No upcoming earnings found — the daily sync populates this once it has run
          (services/scheduler.py::run_earnings_calendar_sync_job).
        </p>
        <p className="text-sm text-muted" style={{ margin: "0.5rem 0 0" }}>
          Provider: {providerLabel(core.earningsCalendarStatus?.primary ?? null)}
          {" · "}
          Last successful sync: {lastSync ? formatRelativeTime(lastSync) : "never"}
        </p>
      </div>
    );
  }

  return (
    <>
    <ListToolbar controls={cards} placeholder="Search ticker or company" testId="upcoming-controls" />
    <div className="grid grid-3">
      {cards.visible.map((event) => {
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
    <Pager controls={cards} testId="upcoming-pager" />
    </>
  );
}

// --------------------------------------------------------------------------
// Section B -- AI Decisions
// --------------------------------------------------------------------------

function riskRewardCell(
  entry: EntryCaptureAttempt | undefined,
  stage: ReturnType<typeof deriveLifecycleStage>
) {
  if (entry && entry.status === "captured") {
    return (
      <span className="mono">
        Risk {entry.initial_max_risk ? `$${formatMoney(entry.initial_max_risk)}` : "—"}
        {" · "}
        {entry.capital_utilization ? `${formatMoney(entry.capital_utilization, 1)}%` : "—"} of
        capital
      </span>
    );
  }
  if (stage === "no_action") {
    return <span className="text-faint">No strategy recommended</span>;
  }
  if (stage === "entry_failed") {
    return <span className="text-faint">Entry capture failed</span>;
  }
  return <span className="text-faint">Awaiting entry</span>;
}

function AiDecisionsSection({ core }: { core: DashboardCore }) {
  // Post-live correction (2026-08-25): every real decision, not just
  // ones with a recommended strategy -- a no-action decision
  // (strategy_type/legs both null, see services/decision_snapshot_
  // freezing.py) is a real, honest outcome that used to be silently
  // dropped from this table entirely.
  const researched = core.decisions;
  const controls = useListControls({
    rows: researched,
    urlKey: "dec",
    searchKeys: [(d) => d.ticker, (d) => d.strategy_type, (d) => d.strategy_direction],
    facet: { label: "Direction", getValue: (d) => d.strategy_direction },
    defaultPageSize: 25,
  });
  const entriesByDecisionId = new Map<number, EntryCaptureAttempt[]>();
  for (const e of core.entries) {
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
      <ListToolbar controls={controls} placeholder="Search ticker, strategy or direction" testId="decisions-controls" />
      <table>
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Strategy</th>
            <th>Direction</th>
            <th title={HISTORICAL_MOVE_COMPATIBILITY_EXPLANATION}>Hist. Compatibility</th>
            <th>Risk / Reward</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {controls.visible.map((d) => {
            const entries = entriesByDecisionId.get(d.id) ?? [];
            const settlements = settlementsByDecisionId.get(d.id) ?? [];
            const stage = deriveLifecycleStage(entries, settlements, d);
            const capturedEntry = entries.find((e) => e.status === "captured");
            return (
              <tr key={d.id}>
                <td className="mono">
                  <Link to={`/earnings-calendar/${d.ticker}`} className="text-link">
                    {d.ticker}
                  </Link>
                </td>
                <td className="mono">{d.strategy_type ?? "—"}</td>
                <td className="mono">{d.strategy_direction}</td>
                <td className="mono">
                  <HistoricalCompatibilityValue snapshot={d} compact />
                </td>
                <td>{riskRewardCell(capturedEntry, stage)}</td>
                <td>
                  <span className={`pill ${LIFECYCLE_PILL_CLASS[stage]}`}>
                    {LIFECYCLE_LABELS[stage]}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <Pager controls={controls} testId="decisions-pager" />
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
  // V4 consolidation, Section 18 -- the TODAY / V4 / readiness header reads
  // its own endpoints and renders immediately. Only the V3-era sections
  // below wait on the (heavier) V3 dashboard core, so a slow control-cohort
  // query can never blank the primary V4 terminal view.
  return (
    <div>
      <div className="page-header">
        <h1>Dashboard</h1>
        <p>
          Real, prospective forward-test evidence. V4 is the experimental engine under test; V3 is the
          historical control. Nothing here is estimated or back-filled.
        </p>
      </div>
      <DashboardV4Header />
      <TickerSearchBar />
      <V3DashboardSections />
    </div>
  );
}

function V3DashboardSections() {
  const core = useAsync(fetchDashboardCore, []);

  if (core.loading && !core.data) return <LoadingState label="Loading V3 control sections…" />;
  if (core.error && !core.data) return <ErrorState message={core.error} />;
  if (!core.data) return null;

  return (
    <>


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
    </>
  );
}
