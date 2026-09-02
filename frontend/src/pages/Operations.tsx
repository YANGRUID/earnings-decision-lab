import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { ListToolbar, PageOutline, Pager } from "../components/ListControls";
import { useListControls } from "../hooks/useListControls";
import { invalidateStatus } from "../lib/statusCache";
import { providerLabel } from "../lib/format";
import { JOB_LABELS, countdown, formatDateTime, stateLabel } from "../lib/operationsFormat";
import type {
  AiProviderHealth,
  FailureEntry,
  HealthState,
  JobStaleness,
  MarketClock,
  PipelineEvent,
  PreflightReadiness,
  PreparationProgress,
  ResearchReadiness,
  SchedulerJobView,
  SystemHealth,
  V4ForwardHealth,
  V4TodaySummary,
} from "../types/api";

// Live Operations -- V4-only reset (2026-09-02). Every value is real,
// already-persisted state from /operations/*, refreshed every 30 s. The
// page renders progressively: the summary paints first, and the pipeline,
// jobs, failures and preparation progress fill in as they arrive.
const POLL_INTERVAL_MS = 30_000;

const OPS_SECTIONS = [
  { id: "ops-system", label: "System" },
  { id: "ops-today", label: "Today" },
  { id: "ops-readiness", label: "Readiness" },
  { id: "ops-pipeline", label: "V4 pipeline" },
  { id: "ops-research-prep", label: "Research prep" },
  { id: "ops-jobs", label: "Scheduler jobs" },
  { id: "ops-attention", label: "Attention" },
  { id: "ops-v4", label: "V4 forward engine" },
];

const TIMING_LABELS: Record<string, string> = {
  bmo: "Before Market Open",
  amc: "After Market Close",
  dmh: "During Market Hours",
  unknown: "Timing Unknown",
};

const STATE_PILL: Record<string, string> = {
  CALENDAR_DISCOVERED: "neutral",
  BUSINESS_INELIGIBLE: "neutral",
  COMPANY_RESOLUTION_FAILED: "negative",
  RESEARCH_QUEUED: "warning",
  RESEARCH_RUNNING: "warning",
  RESEARCH_READY: "positive",
  RESEARCH_FAILED: "negative",
  RESEARCH_NOT_READY: "warning",
  WAITING_DECISION: "warning",
  DECISION_WINDOW_MISSED: "negative",
  DECISION_FAILED: "negative",
  DEADLINE_SKIPPED: "negative",
  NO_ACTION: "neutral",
  ENTRY_OBSERVED: "positive",
  ENTRY_FAILED: "negative",
  WAITING_SETTLEMENT: "warning",
  SETTLED: "positive",
  SETTLEMENT_FAILED: "negative",
};

function usePolling(reloads: Array<() => void>) {
  useEffect(() => {
    const id = setInterval(() => {
      invalidateStatus();
      for (const reload of reloads) reload();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

function formatTime(iso: string | null, timeZone: string): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString(undefined, { timeZone, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function healthPillClass(state: HealthState | string): string {
  if (state === "green") return "positive";
  if (state === "yellow") return "warning";
  if (state === "red") return "negative";
  return "neutral";
}

function HealthPill({ state }: { state: HealthState | string }) {
  return <span className={`pill pill-${healthPillClass(state)}`}>{state.toUpperCase()}</span>;
}

// ---------------------------------------------------------------------------
// Banners
// ---------------------------------------------------------------------------

function PreflightBanner({ preflight }: { preflight: PreflightReadiness }) {
  return (
    <div className={preflight.ready ? "notice-success" : "notice-critical"} data-testid="preflight-banner">
      {preflight.ready ? "READY FOR TODAY'S FORWARD TEST" : `NOT READY — ${preflight.blockers.join("; ")}`}
    </div>
  );
}

function CriticalAlertBanner({ health, staleness, failures, now }: { health: SystemHealth; staleness: JobStaleness[]; failures: FailureEntry[]; now: string }) {
  const alerts: { level: "critical" | "warning"; text: string }[] = [];
  if (!health.scheduler.running) alerts.push({ level: "critical", text: "Scheduler not running." });
  for (const s of staleness) {
    if (s.state === "missed") alerts.push({ level: "critical", text: `MISSED RUN — ${JOB_LABELS[s.job_id] ?? s.job_id}: ${s.detail}` });
    else if (s.state === "stale") alerts.push({ level: "warning", text: `STALE — ${JOB_LABELS[s.job_id] ?? s.job_id}: ${s.detail}` });
  }
  const windowMissed = failures.filter((f) => f.retryability === "WINDOW_MISSED");
  if (windowMissed.length > 0) {
    const ids = windowMissed.map((f) => f.symbol ?? f.stage);
    alerts.push({ level: "critical", text: `${windowMissed.length} window missed: ${ids.slice(0, 5).join(", ")}${ids.length > 5 ? `, and ${ids.length - 5} more` : ""}` });
  }
  if (health.ibkr.state === "red") alerts.push({ level: "critical", text: `IBKR market data unavailable${health.ibkr.last_error ? ` — ${health.ibkr.last_error}` : ""}.` });
  if (health.earnings_calendar.state === "red") alerts.push({ level: "warning", text: "Earnings calendar sync failed on its most recent run." });
  if (health.ai_provider.decision_view_config_error) alerts.push({ level: "critical", text: `V4 DecisionView model not configured — ${health.ai_provider.decision_view_config_error}` });
  if (health.scheduler.last_activity_at && new Date(now).getTime() - new Date(health.scheduler.last_activity_at).getTime() > 24 * 3600 * 1000) {
    alerts.push({ level: "warning", text: "No scheduler job has run successfully in over 24 hours." });
  }
  if (alerts.length === 0) return null;
  return (
    <>
      {alerts.map((alert, i) => (
        <div key={i} className={alert.level === "critical" ? "notice-critical" : "notice"}>
          {alert.level === "critical" ? "CRITICAL: " : "WARNING: "}
          {alert.text}
        </div>
      ))}
    </>
  );
}

function marketSessionPillClass(session: string): string {
  if (session === "regular") return "positive";
  if (session === "closed") return "neutral";
  return "warning";
}

function MarketClockRow({ clock }: { clock: MarketClock }) {
  return (
    <div className="card" data-testid="market-clock">
      <div className="grid grid-4">
        <div className="stat">
          <span className="stat-label">New York</span>
          <span className="stat-value small mono">{formatTime(clock.utc_now, "America/New_York")} ET</span>
        </div>
        <div className="stat">
          <span className="stat-label">Zurich</span>
          <span className="stat-value small mono">{formatTime(clock.utc_now, "Europe/Zurich")} CET</span>
        </div>
        <div className="stat">
          <span className="stat-label">US Market</span>
          <span className="stat-value small">
            <span className={`pill pill-${marketSessionPillClass(clock.market_session)}`}>{clock.market_session.replace("_", " ").toUpperCase()}</span>
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Next automatic action</span>
          <span className="stat-value small">
            {clock.next_automatic_action_job_id ? JOB_LABELS[clock.next_automatic_action_job_id] ?? clock.next_automatic_action_job_id : "—"}
            {clock.next_automatic_action_at && <> in {countdown(clock.next_automatic_action_at, clock.utc_now)}</>}
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// System health
// ---------------------------------------------------------------------------

function ibkrDetail(h: SystemHealth["ibkr"]): string {
  const transport = h.provider === "tws" ? "TWS" : "Web Gateway";
  if (!h.connected) return `${transport} · not connected`;
  return `${transport} · ${h.market_data_quality ?? "quality unknown"}`;
}

function aiDetail(ai: AiProviderHealth): string {
  const base = providerLabel(ai.provider);
  if (!ai.configured) return `${base} · not configured`;
  if (ai.decision_view_config_error) return `${base} · V4 view NOT CONFIGURED`;
  return `${base} · V4 view: ${ai.decision_view_model ?? "—"} · thinking ${ai.decision_view_reasoning_effort ?? ai.decision_view_thinking ?? "—"}`;
}

function SystemHealthSection({ health }: { health: SystemHealth }) {
  const v4 = health.v4_shadow;
  const rows: { label: string; state: HealthState | string; detail: string; sub: string | null }[] = [
    { label: "IBKR market data", state: health.ibkr.state, detail: ibkrDetail(health.ibkr), sub: health.ibkr.last_error ?? (health.ibkr.last_heartbeat_at ? `heartbeat ${formatDateTime(health.ibkr.last_heartbeat_at)}` : null) },
    { label: "Earnings calendar", state: health.earnings_calendar.state, detail: `${providerLabel(health.earnings_calendar.active_provider)} · ${health.earnings_calendar.events_received} events`, sub: health.earnings_calendar.last_error ?? `last sync ${formatDateTime(health.earnings_calendar.last_successful_sync_at)} · next ${formatDateTime(health.earnings_calendar.next_scheduled_sync_at)}` },
    { label: "AI provider", state: health.ai_provider.state, detail: aiDetail(health.ai_provider), sub: health.ai_provider.last_error ?? (health.ai_provider.last_successful_generation_at ? `last generation ${formatDateTime(health.ai_provider.last_successful_generation_at)}` : null) },
    { label: "Scheduler", state: health.scheduler.state, detail: health.scheduler.running ? `running · ${health.scheduler.registered_job_count} jobs` : "NOT RUNNING", sub: `last activity ${formatDateTime(health.scheduler.last_activity_at)} · next ${formatDateTime(health.scheduler.next_activity_at)}` },
    { label: "Database", state: health.database.state, detail: health.database.database_healthy ? "healthy" : "unhealthy", sub: health.database.migration_head ? `migration ${health.database.migration_head}` : null },
    { label: "V4 forward engine", state: v4?.state ?? "gray", detail: v4 ? (v4.enabled ? `enabled · decision ${v4.decision_time_et} · settlement ${v4.settlement_time_et}` : "disabled") : "unavailable", sub: v4 ? `${v4.timing_policy_version} · ${v4.engine_version ?? ""}` : null },
  ];
  return (
    <div className="card" id="ops-system" data-testid="system-health">
      <h2>System</h2>
      <table>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td style={{ width: 180 }}>{r.label}</td>
              <td style={{ width: 90 }}><HealthPill state={r.state} /></td>
              <td>
                <div>{r.detail}</div>
                {r.sub && <div className="text-sm text-muted">{r.sub}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Today + readiness
// ---------------------------------------------------------------------------

function Stat({ label, value, sub, mono = true }: { label: string; value: React.ReactNode; sub?: React.ReactNode; mono?: boolean }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value small${mono ? " mono" : ""}`}>{value}</span>
      {sub && <span className="text-faint text-sm">{sub}</span>}
    </div>
  );
}

function TodayCard({ today, v4 }: { today: V4TodaySummary; v4: V4ForwardHealth | null }) {
  return (
    <div className="card" id="ops-today" data-testid="ops-today">
      <h2>Today's V4 window</h2>
      <div className="grid grid-4">
        <Stat label="Decision window" value={`${today.decision_window_et} ET`} sub={`deadline ${today.deadline_et} ET`} />
        <Stat label="Settlement window" value={`${today.settlement_window_et} ET`} sub="first post-earnings trading day" />
        <Stat label="Events in window" value={today.events_in_window} sub={`${today.business_eligible} business eligible`} />
        <Stat label="Research ready" value={today.research_ready} sub={`${today.waiting_decision} waiting for decision`} />
      </div>
      <div className="grid grid-4" style={{ marginTop: 8 }}>
        <Stat label="Decisions today" value={today.decisions_today} sub={`${today.ranked_today} ranked · ${today.no_action_today} no action`} />
        <Stat label="Entries observed" value={today.entries_observed_today} sub={`${today.entries_failed_today} entry failed`} />
        <Stat label="Deadline skipped" value={today.deadline_skipped_today} sub={`${today.research_not_ready_today} research not ready`} />
        <Stat label="Settlements" value={`${today.settled_today} / ${today.settlements_due_today}`} sub={`${today.settlements_failed_today} failed · ${v4?.settlements_due ?? 0} due overall`} />
      </div>
    </div>
  );
}

function ReadinessCard({ readiness }: { readiness: ResearchReadiness }) {
  const r = readiness;
  const ratio = (n: number) => (r.upcoming_events > 0 ? `${Math.round((100 * n) / r.upcoming_events)}%` : "—");
  return (
    <div className="card" id="ops-readiness" data-testid="ops-readiness">
      <h2>Research readiness (next {r.window_days} days)</h2>
      <div className="grid grid-4">
        <Stat label="Upcoming events" value={r.upcoming_events} />
        <Stat label="Business eligible" value={r.business_eligible} sub={ratio(r.business_eligible)} />
        <Stat label="Company resolved" value={r.company_resolved} sub={ratio(r.company_resolved)} />
        <Stat label="Research ready" value={r.research_ready} sub={`${r.research_queued} queued · ${r.research_running} running · ${r.research_failed} failed`} />
      </div>
      <div className="grid grid-4" style={{ marginTop: 8 }}>
        <Stat label="AI thesis ready" value={r.ai_thesis_ready} sub={ratio(r.ai_thesis_ready)} />
        <Stat label="V4 decision ready" value={r.v4_decision_ready} sub={ratio(r.v4_decision_ready)} />
        <Stat label="Next 15:30 ET window" value={r.next_window_at ? formatDateTime(r.next_window_at) : "—"} sub="decision + settlement run together" />
        <Stat label="Ready for next window" value={`${r.next_window_ready} / ${r.next_window_total}`} sub={r.next_window_total > r.next_window_ready ? "research still catching up" : "all candidates ready"} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pipeline
// ---------------------------------------------------------------------------

function PipelineRow({ event, now }: { event: PipelineEvent; now: string }) {
  const [expanded, setExpanded] = useState(false);
  const pillClass = STATE_PILL[event.lifecycle_state] ?? "neutral";
  return (
    <>
      <tr onClick={() => setExpanded((e) => !e)} style={{ cursor: "pointer" }} data-state={event.lifecycle_state}>
        <td className="mono">
          <Link to={`/company/${event.symbol}`} onClick={(e) => e.stopPropagation()}>{event.symbol}</Link>
        </td>
        <td>{event.company_name}</td>
        <td>
          {new Date(`${event.earnings_date}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })} {TIMING_LABELS[event.earnings_timing] ?? event.earnings_timing}
        </td>
        <td><span className={`pill pill-${pillClass}`}>{stateLabel(event.lifecycle_state)}</span></td>
        <td className="text-sm">{event.research_ready ? <span className="pill pill-positive">ready</span> : <span className="pill pill-neutral">not ready</span>}</td>
        <td className="text-sm text-muted">{event.lifecycle_reason ?? "—"}</td>
        <td className="text-sm">
          {event.next_action ?? "—"}
          {event.shadow_decision_id && (
            <>
              {" "}
              <Link className="text-link" to={`/v4-decision-lab/${event.shadow_decision_id}`} onClick={(e) => e.stopPropagation()}>decision →</Link>
            </>
          )}
        </td>
        <td className="text-sm mono">{event.next_action_at ? formatDateTime(event.next_action_at) : "—"}</td>
        <td className="text-sm mono">{event.next_action_at ? countdown(event.next_action_at, now) : "—"}</td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={9} style={{ background: "var(--color-bg)" }}>
            <div style={{ padding: "8px 4px" }}>
              <div className="text-sm text-muted" style={{ marginBottom: 6 }}>
                Entry {formatDateTime(event.entry_timestamp)} ET · Settlement {formatDateTime(event.exit_timestamp)} ET · entries {event.entries_observed} observed / {event.entries_failed} failed · settlements {event.settlements_settled} settled / {event.settlements_failed} failed
              </div>
              {event.timeline.map((step, i) => (
                <div key={i} className="text-sm" style={{ display: "flex", gap: 10, marginBottom: 4, alignItems: "baseline" }}>
                  <span className={`pill pill-${step.status === "done" ? "positive" : step.status === "failed" ? "negative" : step.status === "warning" ? "warning" : "neutral"}`}>{step.status}</span>
                  <span>{step.label}</span>
                  <span className="mono text-muted">{step.at ? formatDateTime(step.at) : "—"}</span>
                  {step.detail && <span className="text-muted">{step.detail}</span>}
                </div>
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function PipelineTable({ events, now }: { events: PipelineEvent[]; now: string }) {
  const controls = useListControls<PipelineEvent>({
    rows: events,
    urlKey: "pipe",
    searchKeys: [(e) => e.symbol, (e) => e.company_name, (e) => e.lifecycle_reason, (e) => e.next_action],
    facet: { label: "State", getValue: (e) => e.lifecycle_state, format: stateLabel },
    sorts: [
      { key: "earnings", label: "Earnings date", compare: (a, b) => a.earnings_date.localeCompare(b.earnings_date) || a.symbol.localeCompare(b.symbol) },
      { key: "cap", label: "Market cap (largest first)", compare: (a, b) => Number(b.market_cap ?? -Infinity) - Number(a.market_cap ?? -Infinity) },
      { key: "ticker", label: "Ticker (A–Z)", compare: (a, b) => a.symbol.localeCompare(b.symbol) },
      { key: "next", label: "Next action (soonest first)", compare: (a, b) => (a.next_action_at ?? "9").localeCompare(b.next_action_at ?? "9") },
    ],
    defaultPageSize: 25,
  });
  if (events.length === 0) {
    return (
      <div className="card" id="ops-pipeline">
        <h2>V4 pipeline</h2>
        <p className="text-sm text-muted" style={{ margin: 0 }}>No earnings events fall inside the V4 pipeline window (2 days back, 7 days ahead).</p>
      </div>
    );
  }
  return (
    <div className="card" id="ops-pipeline" data-testid="ops-pipeline">
      <h2>V4 pipeline</h2>
      <ListToolbar controls={controls} placeholder="Search ticker, company, reason or next action" testId="pipeline-controls" />
      <table>
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Company</th>
            <th>Earnings</th>
            <th>State</th>
            <th>Research</th>
            <th>Reason</th>
            <th>Next action</th>
            <th>Scheduled</th>
            <th>Time remaining</th>
          </tr>
        </thead>
        <tbody>
          {controls.visible.map((event) => <PipelineRow key={event.calendar_event_id} event={event} now={now} />)}
        </tbody>
      </table>
      <Pager controls={controls} testId="pipeline-pager" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Research preparation, jobs, failures, V4 engine
// ---------------------------------------------------------------------------

function secondsLabel(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function PreparationProgressCard({ progress }: { progress: PreparationProgress }) {
  return (
    <div className="card" id="ops-research-prep" data-testid="ops-research-prep">
      <h2>Research Preparation</h2>
      <div className="grid grid-4">
        <Stat label="Queue" value={`${progress.queue_depth} pending`} />
        <Stat label="Completed" value={progress.completed} />
        <Stat label="Failed" value={progress.failed} />
        <Stat label="Worker" value={progress.worker_active ? "1 active" : "idle"} mono={false} />
      </div>
      {progress.worker_active && (
        <div className="grid grid-4">
          <Stat label="Current company" value={progress.current_symbol ?? "—"} mono={false} />
          <Stat label="Current stage" value={`${progress.current_stage ?? "—"}${progress.step_index !== null && progress.step_total !== null ? ` (${progress.step_index} / ${progress.step_total})` : ""}`} mono={false} />
          <Stat label="Heartbeat" value={progress.heartbeat_seconds_ago !== null ? `${Math.floor(progress.heartbeat_seconds_ago)}s ago` : "—"} />
          <Stat label="Elapsed" value={progress.elapsed_seconds !== null ? secondsLabel(progress.elapsed_seconds) : "—"} />
        </div>
      )}
    </div>
  );
}

function stalenessPill(s: JobStaleness | undefined) {
  if (!s) return null;
  if (s.state === "missed") return <span className="pill pill-negative" title={s.detail}>MISSED RUN</span>;
  if (s.state === "stale") return <span className="pill pill-warning" title={s.detail}>STALE</span>;
  if (s.state === "never") return <span className="pill pill-neutral" title={s.detail}>NEVER RAN</span>;
  return <span className="pill pill-positive" title={s.detail}>ON TIME</span>;
}

function SchedulerJobsSection({ jobs, staleness, now }: { jobs: SchedulerJobView[]; staleness: JobStaleness[]; now: string }) {
  const byJob = new Map(staleness.map((s) => [s.job_id, s]));
  return (
    <div className="card" id="ops-jobs" data-testid="ops-jobs">
      <h2>Scheduler Jobs</h2>
      <table>
        <thead>
          <tr>
            <th>Job</th>
            <th>Status</th>
            <th>Freshness</th>
            <th>Last expected</th>
            <th>Last run</th>
            <th>Duration</th>
            <th>Items</th>
            <th>Next run</th>
            <th>Countdown</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => {
            const s = byJob.get(job.job_id);
            return (
              <tr key={job.job_id}>
                <td>{JOB_LABELS[job.job_id] ?? job.job_id}</td>
                <td>
                  {!job.enabled ? <span className="pill pill-neutral">NOT SCHEDULED</span>
                    : job.last_run_status === "success" ? <span className="pill pill-positive">READY</span>
                    : job.last_run_status === "failed" ? <span className="pill pill-negative" title={job.last_error ?? undefined}>FAILED</span>
                    : job.last_run_status === "running" ? <span className="pill pill-warning">RUNNING</span>
                    : <span className="pill pill-neutral">NO RUNS YET</span>}
                </td>
                <td>{stalenessPill(s)}</td>
                <td className="text-sm mono">{s?.last_expected_at ? formatDateTime(s.last_expected_at) : "—"}</td>
                <td className="text-sm mono">{formatDateTime(job.last_run_at)}</td>
                <td className="text-sm mono">{job.duration_ms !== null ? `${(job.duration_ms / 1000).toFixed(1)}s` : "—"}</td>
                <td className="text-sm mono">{job.items_evaluated !== null ? `${job.items_succeeded ?? 0}/${job.items_evaluated}${job.items_failed ? ` (${job.items_failed} failed)` : ""}` : "—"}</td>
                <td className="text-sm mono">{formatDateTime(job.next_run_time)}</td>
                <td className="text-sm mono">{job.next_run_time ? countdown(job.next_run_time, now) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function retryPill(r: FailureEntry["retryability"]) {
  if (r === "RETRYABLE") return <span className="pill pill-warning">RETRYABLE</span>;
  if (r === "WINDOW_MISSED") return <span className="pill pill-negative">WINDOW MISSED</span>;
  return <span className="pill pill-neutral">NOT RETRYABLE</span>;
}

function AttentionSection({ failures }: { failures: FailureEntry[] }) {
  const controls = useListControls<FailureEntry>({
    rows: failures,
    urlKey: "fail",
    searchKeys: [(f) => f.symbol, (f) => f.stage, (f) => f.category, (f) => f.explanation],
    facet: { label: "Stage", getValue: (f) => f.stage },
    defaultPageSize: 25,
  });
  return (
    <div className="card" id="ops-attention" data-testid="ops-attention">
      <h2>Attention</h2>
      {failures.length === 0 ? (
        <p className="text-sm text-muted" style={{ margin: 0 }}>No failures in the recent window.</p>
      ) : (
        <>
          <ListToolbar controls={controls} placeholder="Search symbol, stage or explanation" testId="failure-controls" />
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Symbol</th>
                <th>Stage</th>
                <th>Category</th>
                <th>Explanation</th>
                <th>Retry</th>
              </tr>
            </thead>
            <tbody>
              {controls.visible.map((f, i) => (
                <tr key={`${f.occurred_at}-${f.symbol ?? ""}-${i}`}>
                  <td className="text-sm mono">{formatDateTime(f.occurred_at)}</td>
                  <td className="mono">{f.symbol ? <Link to={`/company/${f.symbol}`}>{f.symbol}</Link> : "—"}</td>
                  <td className="text-sm">{f.stage}</td>
                  <td className="text-sm mono">{f.category}</td>
                  <td className="text-sm">
                    {f.explanation}
                    {f.detail && <div className="text-faint text-sm">{f.detail}</div>}
                  </td>
                  <td>{retryPill(f.retryability)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager controls={controls} testId="failure-pager" />
        </>
      )}
    </div>
  );
}

function V4EngineCard({ v4, ai, jobs }: { v4: V4ForwardHealth | null; ai: AiProviderHealth; jobs: SchedulerJobView[] }) {
  const decision = jobs.find((j) => j.job_id === "v4_shadow_decision") ?? null;
  const settlement = jobs.find((j) => j.job_id === "v4_shadow_settlement") ?? null;
  return (
    <div className="card" id="ops-v4" data-testid="operations-v4">
      <h2>V4 forward engine</h2>
      {!v4 ? (
        <p className="text-sm text-muted">V4 health unavailable.</p>
      ) : (
        <>
          <div className="grid grid-4">
            <Stat label="State" value={v4.enabled ? <span className="pill pill-positive">ENABLED</span> : <span className="pill pill-neutral">DISABLED</span>} mono={false} sub={v4.note} />
            <Stat label="Decision" value={`${v4.decision_time_et} ET`} sub="legal pre-earnings trading day" />
            <Stat label="Settlement" value={`${v4.settlement_time_et} ET`} sub="first post-earnings trading day" />
            <Stat label="Timing policy" value={v4.timing_policy_version} sub={v4.engine_version ?? undefined} />
          </div>
          <div className="grid grid-4" style={{ marginTop: 8 }}>
            <Stat label="Decisions today" value={v4.decisions_today} sub={`${v4.ranked_today} ranked · ${v4.no_action_today} no action · ${v4.failed_today} failed`} />
            <Stat label="Entry observations failed" value={v4.entry_observations_failed_today} />
            <Stat label="Settlements" value={`${v4.settlements_complete} settled · ${v4.settlements_due} due`} />
            <Stat label="Last run" value={formatDateTime(v4.last_run_at)} />
          </div>
        </>
      )}
      <div className="grid grid-4" style={{ gap: 10, marginTop: 10 }} data-testid="operations-v4-model">
        <Stat label="DecisionView model" value={ai.decision_view_model ?? <span className="pill pill-negative">NOT CONFIGURED</span>} />
        <Stat label="Thinking" value={ai.decision_view_thinking ?? "—"} />
        <Stat label="Reasoning effort" value={ai.decision_view_reasoning_effort ?? "—"} />
        <Stat label="Max tokens" value={ai.decision_view_max_tokens ?? "—"} />
      </div>
      {ai.decision_view_config_error && (
        <div className="notice notice-critical" style={{ marginTop: 8 }} data-testid="operations-v4-model-error">{ai.decision_view_config_error}</div>
      )}
      <div className="grid grid-4" style={{ gap: 10, marginTop: 10 }} data-testid="operations-v4-jobs">
        <Stat label="Decision job" value={decision ? formatDateTime(decision.next_run_time) : "not registered"} sub={decision?.last_run_status ? `last ${decision.last_run_status}` : undefined} />
        <Stat label="Settlement job" value={settlement ? formatDateTime(settlement.next_run_time) : "not registered"} sub={settlement?.last_run_status ? `last ${settlement.last_run_status}` : undefined} />
        <Stat label="Order execution" value="none" mono={false} sub="forward test only — no brokerage orders" />
        <Stat label="Evidence" value={<Link className="text-link" to="/v4-shadow-track-record">V4 Forward Track Record →</Link>} mono={false} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function Operations() {
  const summary = useAsync(() => api.getOperationsSummary(), []);
  const events = useAsync((signal) => api.getOperationsEvents({ signal }), []);
  const jobs = useAsync((signal) => api.getOperationsJobs({ signal }), []);
  const failures = useAsync((signal) => api.getOperationsFailures({ signal }), []);
  const preparationProgress = useAsync(() => api.getOperationsPreparationProgress(), []);

  usePolling([summary.reload, events.reload, jobs.reload, failures.reload, preparationProgress.reload]);

  if (summary.loading && !summary.data) return <LoadingState label="Loading operations monitor…" />;
  if (summary.error && !summary.data) return <ErrorState message={summary.error} />;
  if (!summary.data) return null;

  const s = summary.data;
  const now = s.market_clock.utc_now;
  const prep = preparationProgress.data;
  const showPrep = prep && (prep.worker_active || prep.queue_depth > 0 || prep.completed > 0 || prep.failed > 0);

  return (
    <div>
      <div className="page-header">
        <h1>Live Operations</h1>
        <p>
          Real-time visibility into the V4 forward test — calendar discovery, research preparation,
          the 15:30 ET decision, entry evidence and the 15:30 ET T+1 settlement. Every value is real,
          already-persisted state, refreshed every 30 seconds. No orders are ever placed.
        </p>
      </div>
      <PageOutline sections={OPS_SECTIONS} />

      <PreflightBanner preflight={s.preflight} />
      <CriticalAlertBanner health={s.health} staleness={s.staleness} failures={failures.data?.failures ?? []} now={now} />

      <MarketClockRow clock={s.market_clock} />
      <SystemHealthSection health={s.health} />
      <TodayCard today={s.today} v4={s.health.v4_shadow} />
      <ReadinessCard readiness={s.readiness} />

      {events.data ? <PipelineTable events={events.data.events} now={now} /> : events.error ? <ErrorState message={events.error} /> : <LoadingState label="Loading V4 pipeline…" />}
      {showPrep && prep && <PreparationProgressCard progress={prep} />}
      {jobs.data ? <SchedulerJobsSection jobs={jobs.data.jobs} staleness={s.staleness} now={now} /> : jobs.error ? <ErrorState message={jobs.error} /> : <LoadingState label="Loading scheduler jobs…" />}
      {failures.data ? <AttentionSection failures={failures.data.failures} /> : failures.error ? <ErrorState message={failures.error} /> : null}
      <V4EngineCard v4={s.health.v4_shadow} ai={s.health.ai_provider} jobs={jobs.data?.jobs ?? []} />
    </div>
  );
}
