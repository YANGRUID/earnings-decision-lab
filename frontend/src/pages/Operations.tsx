import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { OperationsV4Section } from "../components/v4/OperationsV4Section";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { providerLabel } from "../lib/format";
import type {
  ExecutionSummary,
  FailureEntry,
  HealthState,
  MarketClock,
  PipelineEvent,
  PreflightReadiness,
  PreparationProgress,
  QuoteDiagnosticsSummary,
  SchedulerJobView,
  SystemHealth,
  TodaysOfficialRun,
} from "../types/api";

// Section 19 -- real-time refresh, no websockets (none exist in this
// codebase yet). 30s (the top of the user's own suggested 10-30s range),
// not 15s: this page makes 4 real backend calls per tick, and a live
// test found that polling this often -- on top of the scheduler's own
// jobs sharing the same backend process's DB connection pool -- can
// starve a scheduled job of a connection long enough to delay it by
// several fire cycles with nothing logged anywhere. See db/session.py's
// own pool_size/max_overflow comment for the other half of this fix.
const POLL_INTERVAL_MS = 30_000;

const JOB_LABELS: Record<string, string> = {
  earnings_calendar_sync: "Earnings Calendar Sync",
  earnings_research_preparation: "Earnings Research Preparation",
  decision_and_entry_capture: "Decision + Entry Capture",
  exit_capture: "Exit Capture",
  // V4 shadow cohort (experimental). Registered only while V4_SHADOW_ENABLED
  // is on; listed here because the job monitor reports every registered job.
  v4_shadow_decision: "V4 Shadow Decision (experimental, 15:30 ET)",
  v4_shadow_settlement: "V4 Shadow Settlement (experimental, 15:55 ET)",
  // IBKR TWS Migration, Phase 3 readiness (Section 29) -- display label
  // only, provider-neutral now that this job runs against either
  // transport (see services/scheduler.py::run_ibkr_gateway_healthcheck_job).
  // The persisted job_id itself stays "ibkr_gateway_healthcheck" -- never
  // renamed, so existing SchedulerRun history isn't disturbed.
  ibkr_gateway_healthcheck: "IBKR Provider Healthcheck",
};

const TIMING_LABELS: Record<string, string> = {
  bmo: "Before Market Open",
  amc: "After Market Close",
  dmh: "During Market Hours",
  unknown: "Timing Unknown",
};

// One shared timer for all 4 endpoints, not 4 independent ones -- 4
// separate setIntervals started at the same mount time fire in the same
// simultaneous burst every cycle anyway, so splitting them gains nothing
// and only makes the actual request volume harder to reason about.
function usePolling(reloads: Array<() => void>) {
  useEffect(() => {
    const id = setInterval(() => {
      for (const reload of reloads) reload();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatTime(iso: string | null, timeZone: string): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString(undefined, {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function countdown(targetIso: string | null, nowIso: string): string {
  if (!targetIso) return "—";
  const diffMs = new Date(targetIso).getTime() - new Date(nowIso).getTime();
  if (diffMs <= 0) return "due now";
  const totalMinutes = Math.floor(diffMs / 60000);
  const days = Math.floor(totalMinutes / (60 * 24));
  const hours = Math.floor((totalMinutes % (60 * 24)) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  return `${hours}h ${minutes}m`;
}

function healthDotColor(state: HealthState): string {
  switch (state) {
    case "green":
      return "var(--color-positive)";
    case "yellow":
      return "var(--color-warning-text)";
    case "red":
      return "var(--color-negative)";
    default:
      return "var(--color-text-faint)";
  }
}

function HealthPill({ label, state, detail }: { label: string; state: HealthState; detail?: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value small" style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          style={{
            display: "inline-block",
            width: 9,
            height: 9,
            borderRadius: "50%",
            background: healthDotColor(state),
            flexShrink: 0,
          }}
        />
        {state.toUpperCase()}
      </span>
      {detail && (
        <span className="text-sm text-muted" style={{ marginTop: 2 }}>
          {detail}
        </span>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Section 9 -- Critical Alert Banner
// --------------------------------------------------------------------------

// Categories detect_missed_job_alerts() (backend, services/operations.py)
// produces -- surfaced here as CRITICAL rather than in the Failure
// Center table alone, per Section 7's "must surface prominently" ask.
// Deliberately backend-authoritative, not re-derived client-side: the
// backend already cross-references scheduler_jobs/pipeline_events/health
// against real thresholds (MISSED_JOB_GRACE, STUCK_RUN_THRESHOLD,
// IBKR_OUTAGE_LOOKAHEAD) -- this page must never compute a second,
// possibly-disagreeing version of the same judgment.
const CRITICAL_FAILURE_CATEGORIES = new Set([
  "missed_job",
  "job_running_too_long",
  "unprocessed_due_event",
  "ibkr_unavailable_before_entry",
]);

const CRITICAL_CATEGORY_LABELS: Record<string, string> = {
  missed_job: "scheduler job(s) overdue",
  job_running_too_long: "scheduler job(s) still running past a reasonable duration",
  unprocessed_due_event: "due event(s) show no decision/entry activity yet",
  ibkr_unavailable_before_entry: "IBKR unavailable with entry work due soon",
};

// One summarized line per category, not one line per FailureEntry --
// a bad calendar sync day (or, as observed live, a full day's worth of
// due-but-unprocessed events before the forward test had actually
// started) can produce dozens of individually-real alerts; stacking all
// of them as separate top-of-page banners is technically accurate but
// drowns the page and trains the reader to stop looking, defeating the
// whole point of "surface prominently."
function summarizeCriticalFailures(failures: FailureEntry[]): string[] {
  const byCategory = new Map<string, FailureEntry[]>();
  for (const failure of failures) {
    if (!CRITICAL_FAILURE_CATEGORIES.has(failure.category)) continue;
    const group = byCategory.get(failure.category) ?? [];
    group.push(failure);
    byCategory.set(failure.category, group);
  }
  const lines: string[] = [];
  for (const [category, group] of byCategory) {
    if (group.length === 1) {
      lines.push(group[0].explanation);
      continue;
    }
    const identifiers = group.map((f) => f.symbol ?? f.stage);
    const shown = identifiers.slice(0, 5).join(", ");
    const remainder = identifiers.length - 5;
    const label = CRITICAL_CATEGORY_LABELS[category] ?? category;
    lines.push(
      `${group.length} ${label}: ${shown}${remainder > 0 ? `, and ${remainder} more` : ""}`
    );
  }
  return lines;
}

function CriticalAlertBanner({
  health,
  failures,
  now,
}: {
  health: SystemHealth;
  failures: FailureEntry[];
  now: string;
}) {
  const alerts: { level: "critical" | "warning"; text: string }[] = [];

  if (!health.scheduler.running) {
    alerts.push({ level: "critical", text: "Scheduler not running." });
  }
  for (const text of summarizeCriticalFailures(failures)) {
    alerts.push({ level: "critical", text });
  }
  if (health.ibkr.state === "green" && health.ibkr.live_account === false) {
    alerts.push({ level: "warning", text: "IBKR is connected to a PAPER account, not LIVE." });
  }
  if (health.earnings_calendar.state === "red") {
    alerts.push({ level: "warning", text: "EarningsAPI sync failed on its most recent run." });
  }
  if (
    health.scheduler.last_activity_at &&
    new Date(now).getTime() - new Date(health.scheduler.last_activity_at).getTime() > 24 * 3600 * 1000
  ) {
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

// --------------------------------------------------------------------------
// Section 10 -- Pre-Flight Readiness banner
// --------------------------------------------------------------------------

function PreflightBanner({ preflight }: { preflight: PreflightReadiness }) {
  return (
    <div className={preflight.ready ? "notice-success" : "notice-critical"}>
      {preflight.ready
        ? "READY FOR TODAY'S FORWARD TEST"
        : `NOT READY — ${preflight.blockers.join("; ")}`}
    </div>
  );
}

// --------------------------------------------------------------------------
// Section 11 -- Market Clock
// --------------------------------------------------------------------------

function MarketClockRow({ clock }: { clock: MarketClock }) {
  return (
    <div className="card">
      <div className="grid grid-4">
        <div className="stat">
          <span className="stat-label">New York</span>
          <span className="stat-value small mono">
            {formatTime(clock.utc_now, "America/New_York")} ET
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Zurich</span>
          <span className="stat-value small mono">
            {formatTime(clock.utc_now, "Europe/Zurich")} CET
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">US Market</span>
          <span className="stat-value small">
            <span className={`pill pill-${marketSessionPillClass(clock.market_session)}`}>
              {clock.market_session.replace("_", " ").toUpperCase()}
            </span>
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Next automatic action</span>
          <span className="stat-value small">
            {clock.next_automatic_action_job_id
              ? JOB_LABELS[clock.next_automatic_action_job_id] ?? clock.next_automatic_action_job_id
              : "—"}
            {clock.next_automatic_action_at && (
              <>
                {" in "}
                {countdown(clock.next_automatic_action_at, clock.utc_now)}
              </>
            )}
          </span>
        </div>
      </div>
    </div>
  );
}

function marketSessionPillClass(session: string): string {
  if (session === "regular") return "positive";
  if (session === "closed") return "neutral";
  return "warning";
}

// --------------------------------------------------------------------------
// Section 2 -- System Health
// --------------------------------------------------------------------------

// IBKR TWS Migration, Phase 3 readiness (Section 25) -- provider-aware,
// one-line summary for the compact System grid below (the full
// connection detail lives on Settings -> IBKR). LIVE ACCOUNT and DELAYED
// MARKET DATA are kept as separate concepts per Section 25's own
// explicit instruction, never merged into one claim -- and TWS's real,
// structural inability to report live/paper (live_account stays null,
// see services/operations.py::get_system_health's TWS branch) is shown
// as exactly that, never silently implied to be a paper account.
function ibkrHealthDetail(ibkr: SystemHealth["ibkr"]): string | undefined {
  if (ibkr.last_error) return ibkr.last_error;
  const providerLabel = ibkr.provider === "tws" ? "TWS" : "Web";
  const accountLabel =
    ibkr.live_account === true ? "live" : ibkr.live_account === false ? "paper" : null;
  const parts = [providerLabel, accountLabel, ibkr.market_data_quality ?? (ibkr.connected ? "awaiting first market-data observation" : null)].filter(
    (p): p is string => Boolean(p)
  );
  return parts.length > 0 ? parts.join(" · ") : undefined;
}

function SystemHealthSection({ health }: { health: SystemHealth }) {
  return (
    <div className="card">
      <h2>System</h2>
      <div className="grid grid-4">
        <HealthPill label="IBKR" state={health.ibkr.state} detail={ibkrHealthDetail(health.ibkr)} />
        <HealthPill
          label="Earnings Calendar"
          state={health.earnings_calendar.state}
          detail={
            health.earnings_calendar.active_provider
              ? providerLabel(health.earnings_calendar.active_provider)
              : "Not configured"
          }
        />
        <HealthPill
          label="AI Provider"
          state={health.ai_provider.state}
          detail={providerLabel(health.ai_provider.provider)}
        />
        <HealthPill
          label="Scheduler"
          state={health.scheduler.state}
          detail={`${health.scheduler.registered_job_count} jobs registered`}
        />
        <HealthPill
          label="Database / Backend"
          state={health.database.state}
          detail={
            !health.database.backend_healthy
              ? "Backend unhealthy"
              : !health.database.database_healthy
                ? "Database unhealthy"
                : health.database.migration_head
                  ? `Migrated (${health.database.migration_head.slice(0, 8)})`
                  : "Healthy"
          }
        />
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Post-official-run cleanup (2026-08-27), Section 3/4 -- Today's Official
// Run: sourced strictly from today's real, persisted SchedulerRun/
// SchedulerRunEvent rows, never from the broader pipeline table below.
// --------------------------------------------------------------------------

function TodaysOfficialRunCard({ run }: { run: TodaysOfficialRun }) {
  if (!run.found) {
    return (
      <div className="card">
        <h2>Today's Official Run</h2>
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          The scheduler hasn't fired today's decision/entry run yet — an honest, empty state,
          not an error.
        </p>
      </div>
    );
  }
  const items: [string, number][] = [
    ["Evaluated", run.evaluated],
    ["Ineligible / Skipped", run.skipped_ineligible],
    ["Decisions Created", run.decisions_created],
    ["No Action", run.no_action],
    ["Entries Captured", run.entries_captured],
    ["Entries Failed", run.entries_failed],
    ["Pipeline Failed", run.pipeline_failed],
    ["Settlements Captured", run.settlements_captured],
    ["Settlements Failed", run.settlements_failed],
  ];
  return (
    <div className="card">
      <h2>Today's Official Run</h2>
      <p className="text-sm text-faint" style={{ marginTop: -4 }}>
        Run started {formatDateTime(run.run_started_at)}
        {run.run_finished_at ? `, finished ${formatDateTime(run.run_finished_at)}` : " — running"}
        {" · "}
        {run.run_status}
      </p>
      <div className="grid grid-4">
        {items.map(([label, value]) => (
          <div className="stat" key={label}>
            <span className="stat-label">{label}</span>
            <span className="stat-value small">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Section 7 -- Current Pipeline Summary. A wider, real but multi-day view
// (get_todays_pipeline's own window) -- deliberately NOT called "Today's"
// (post-official-run cleanup, 2026-08-27, Section 3): it includes any
// event whose own computed entry OR exit timestamp lands on today's NY
// date, drawn from several days of real calendar events either side of
// today, not only what the scheduler actually touched today. See
// TodaysOfficialRunCard above for that.
// --------------------------------------------------------------------------

function ExecutionSummaryCard({ summary }: { summary: ExecutionSummary }) {
  const items: [string, number][] = [
    ["Pipeline Events", summary.todays_events],
    ["Eligibility Passed", summary.eligibility_passed],
    ["Eligibility Failed", summary.eligibility_failed],
    ["Decisions Created", summary.decisions_created],
    ["Waiting For Entry", summary.waiting_for_entry],
    ["Entries Captured", summary.entries_captured],
    ["Entry Failures", summary.entry_failures],
    ["Settlements Due", summary.settlements_due],
    ["Settled", summary.settled],
    ["Settlement Failures", summary.settlement_failures],
  ];
  return (
    <div className="card">
      <h2>Current Pipeline Summary</h2>
      <p className="text-sm text-faint" style={{ marginTop: -4 }}>
        Every real event whose own entry or exit lands on today's date, across the wider tracked
        pipeline — not only what today's official scheduler run touched.
      </p>
      <div className="grid grid-4">
        {items.map(([label, value]) => (
          <div className="stat" key={label}>
            <span className="stat-label">{label}</span>
            <span className="stat-value small">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Section 3/4/5 -- Today's Pipeline
// --------------------------------------------------------------------------

const LIFECYCLE_PILL: Record<string, string> = {
  SETTLED: "positive",
  ENTRY_CAPTURED: "positive",
  READY_FOR_DECISION: "positive",
  WAITING_FOR_SETTLEMENT: "neutral",
  WAITING_FOR_ENTRY: "warning",
  WAITING_FOR_DECISION: "warning",
  DECISION_GENERATED: "neutral",
  CALENDAR_DISCOVERED: "neutral",
  // Post-live correction (2026-08-25): a real, non-error terminal
  // outcome (the strategy engine found nothing actionable) -- never the
  // same red as an infrastructure failure.
  NO_ACTION: "neutral",
  NOT_ELIGIBLE: "negative",
  SKIPPED: "negative",
  ENTRY_FAILED: "negative",
  SETTLEMENT_FAILED: "negative",
  FILTERED_OUT: "negative",
  PREPARATION_FAILED: "negative",
};

function PipelineRow({ event, now }: { event: PipelineEvent; now: string }) {
  const [expanded, setExpanded] = useState(false);
  const pillClass = LIFECYCLE_PILL[event.lifecycle_state] ?? "neutral";

  return (
    <>
      <tr onClick={() => setExpanded((e) => !e)} style={{ cursor: "pointer" }}>
        <td className="mono">
          <Link to={`/company/${event.symbol}`} onClick={(e) => e.stopPropagation()}>
            {event.symbol}
          </Link>
        </td>
        <td>{event.company_name}</td>
        <td>
          {new Date(`${event.earnings_date}T00:00:00`).toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
          })}{" "}
          {TIMING_LABELS[event.earnings_timing] ?? event.earnings_timing}
        </td>
        <td>
          <span className={`pill pill-${pillClass}`}>{event.lifecycle_state.replace(/_/g, " ")}</span>
        </td>
        <td className="text-sm text-muted">{event.lifecycle_reason ?? "—"}</td>
        <td className="text-sm">{event.next_action ?? "—"}</td>
        <td className="text-sm mono">
          {event.next_action_at ? formatDateTime(event.next_action_at) : "—"}
        </td>
        <td className="text-sm mono">
          {event.next_action_at ? countdown(event.next_action_at, now) : "—"}
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={8} style={{ background: "var(--color-bg)" }}>
            <div style={{ padding: "8px 4px" }}>
              {event.timeline.map((step, i) => (
                <div
                  key={i}
                  className="text-sm"
                  style={{ display: "flex", gap: 10, marginBottom: 4, alignItems: "baseline" }}
                >
                  <span
                    className={
                      step.status === "done"
                        ? "positive"
                        : step.status === "failed"
                          ? "negative"
                          : step.status === "warning"
                            ? "warning"
                            : "text-faint"
                    }
                    style={{ width: 16, flexShrink: 0 }}
                  >
                    {step.status === "done"
                      ? "✓"
                      : step.status === "failed"
                        ? "✕"
                        : step.status === "warning"
                          ? "⚠"
                          : "○"}
                  </span>
                  <span className="mono text-muted" style={{ width: 140, flexShrink: 0 }}>
                    {step.at ? formatDateTime(step.at) : "pending"}
                  </span>
                  <span>{step.label}</span>
                  {step.detail && <span className="text-muted">— {step.detail}</span>}
                </div>
              ))}
              {event.decision_snapshot_id && (
                <Link
                  to={`/company/${event.symbol}`}
                  className="text-link text-sm"
                  style={{ display: "inline-block", marginTop: 6 }}
                >
                  View decision in company workspace →
                </Link>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function TodaysPipeline({ events, now }: { events: PipelineEvent[]; now: string }) {
  if (events.length === 0) {
    return (
      <div className="card">
        <h2>Today's Earnings Pipeline</h2>
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          No real earnings events fall inside the current pipeline window.
        </p>
      </div>
    );
  }
  return (
    <div className="card">
      <h2>Today's Earnings Pipeline</h2>
      <table>
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Company</th>
            <th>Earnings</th>
            <th>Lifecycle</th>
            <th>Reason</th>
            <th>Next Action</th>
            <th>Scheduled</th>
            <th>Time Remaining</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <PipelineRow key={event.calendar_event_id} event={event} now={now} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------
// Pre-live hardening (2026-08-25) -- live state of the durable research-
// preparation queue. Rendered whenever there's a queue-managed row to
// show (queue depth, or a worker actively claimed on one) -- see the
// call site. Enqueueing itself is now near-instant, so the real,
// possibly-minutes-long work only ever shows up here as a currently-
// claimed job, never as a "running" scheduler job.
// --------------------------------------------------------------------------

function secondsLabel(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(s / 60);
  const seconds = s % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function PreparationProgressCard({ progress }: { progress: PreparationProgress }) {
  return (
    <div className="card">
      <h2>Research Preparation</h2>
      <div className="grid grid-4">
        <div className="stat">
          <span className="stat-label">Queue</span>
          <span className="stat-value small mono">{progress.queue_depth} pending</span>
        </div>
        <div className="stat">
          <span className="stat-label">Completed</span>
          <span className="stat-value small mono">{progress.completed}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Failed</span>
          <span className="stat-value small mono">{progress.failed}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Worker</span>
          <span className="stat-value small">
            {progress.worker_active ? "1 active" : "idle"}
          </span>
        </div>
      </div>
      {progress.worker_active && (
        <div className="grid grid-4">
          <div className="stat">
            <span className="stat-label">Current company</span>
            <span className="stat-value small">{progress.current_symbol ?? "—"}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Current stage</span>
            <span className="stat-value small">
              {progress.current_stage ?? "—"}
              {progress.step_index !== null && progress.step_total !== null
                ? ` (${progress.step_index} / ${progress.step_total})`
                : ""}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Heartbeat</span>
            <span className="stat-value small mono">
              {progress.heartbeat_seconds_ago !== null
                ? `${Math.floor(progress.heartbeat_seconds_ago)}s ago`
                : "—"}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Elapsed</span>
            <span className="stat-value small mono">
              {progress.elapsed_seconds !== null ? secondsLabel(progress.elapsed_seconds) : "—"}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Section 6 -- Scheduler Job Monitor
// --------------------------------------------------------------------------

function SchedulerJobsSection({ jobs, now }: { jobs: SchedulerJobView[]; now: string }) {
  return (
    <div className="card">
      <h2>Scheduler Jobs</h2>
      <table>
        <thead>
          <tr>
            <th>Job</th>
            <th>Status</th>
            <th>Last Run</th>
            <th>Duration</th>
            <th>Items</th>
            <th>Next Run</th>
            <th>Countdown</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.job_id}>
              <td>{JOB_LABELS[job.job_id] ?? job.job_id}</td>
              <td>
                {!job.enabled ? (
                  <span className="pill pill-neutral">NOT SCHEDULED</span>
                ) : job.last_run_status === "success" ? (
                  <span className="pill pill-positive">READY</span>
                ) : job.last_run_status === "error" ? (
                  <span className="pill pill-negative">FAILED</span>
                ) : job.last_run_status === "skipped" ? (
                  <span className="pill pill-neutral">SKIPPED</span>
                ) : (
                  <span className="pill pill-neutral">NO RUNS YET</span>
                )}
              </td>
              <td className="text-sm mono">{formatDateTime(job.last_run_at)}</td>
              <td className="text-sm mono">
                {job.duration_ms !== null ? `${(job.duration_ms / 1000).toFixed(1)}s` : "—"}
              </td>
              <td className="text-sm mono">
                {job.items_evaluated !== null
                  ? `${job.items_succeeded ?? 0}/${job.items_evaluated} ok, ${job.items_failed ?? 0} failed`
                  : "—"}
              </td>
              <td className="text-sm mono">{formatDateTime(job.next_run_time)}</td>
              <td className="text-sm mono">{countdown(job.next_run_time, now)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------
// Section 8 -- Failure Center
// --------------------------------------------------------------------------

function FailureCenter({ failures }: { failures: FailureEntry[] }) {
  if (failures.length === 0) {
    return (
      <div className="card">
        <h2>Attention Required</h2>
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          No real failures in the last 3 days.
        </p>
      </div>
    );
  }
  return (
    <div className="card">
      <h2>Attention Required</h2>
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Ticker</th>
            <th>Stage</th>
            <th>Explanation</th>
            <th>Retryability</th>
          </tr>
        </thead>
        <tbody>
          {failures.map((f, i) => (
            <tr key={i}>
              <td className="text-sm mono">{formatDateTime(f.occurred_at)}</td>
              <td className="mono">{f.symbol ?? "—"}</td>
              <td className="text-sm">{f.stage}</td>
              <td className="text-sm">
                {f.explanation}
                {f.detail && <div className="text-muted">{f.detail}</div>}
              </td>
              <td>
                <span
                  className={`pill ${
                    f.retryability === "RETRYABLE"
                      ? "pill-warning"
                      : f.retryability === "WINDOW_MISSED"
                        ? "pill-neutral"
                        : "pill-negative"
                  }`}
                >
                  {f.retryability.replace("_", " ")}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------
// Quote-acquisition diagnostics (Phase 4 quote-observability hardening,
// 2026-08-26, Section 14) -- a compact, bounded aggregate over recent
// QuoteAcquisitionAttempt telemetry: how the acquisition PROCESS
// behaved (poll counts, provider-exception categories), never a
// trading-performance metric. Deliberately its own separate card after
// the Failure Center, not folded into ExecutionSummaryCard or the main
// pipeline table -- Section 13's own "don't clutter the main pipeline."
// --------------------------------------------------------------------------

function QuoteDiagnosticsSummaryCard({ summary }: { summary: QuoteDiagnosticsSummary }) {
  const items: [string, string | number][] = [
    ["Contracts Resolved", `${summary.contracts_resolved}/${summary.contracts_requested}`],
    ["Snapshot Attempts", summary.total_snapshot_attempts],
    ["Avg Attempts / Leg", summary.average_attempts_per_leg ?? "—"],
    ["Median Attempts / Leg", summary.median_attempts_per_leg ?? "—"],
    ["Quote Unavailable", summary.quote_unavailable_count],
    ["Rate Limited", summary.rate_limited_count],
    ["Permission Error", summary.permission_error_count],
    ["Contract Error", summary.contract_error_count],
  ];
  return (
    <div className="card">
      <h2>Quote-Acquisition Diagnostics</h2>
      <p className="text-sm text-muted" style={{ marginTop: 0 }}>
        Real, bounded telemetry from the last {summary.window_hours}h of entry/settlement quote
        polling — how the acquisition process behaved, never a trading-performance metric.
      </p>
      <div className="grid grid-4">
        {items.map(([label, value]) => (
          <div className="stat" key={label}>
            <span className="stat-label">{label}</span>
            <span className="stat-value small">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Operations() {
  const summary = useAsync(() => api.getOperationsSummary(), []);
  const events = useAsync(() => api.getOperationsEvents(), []);
  const jobs = useAsync(() => api.getOperationsJobs(), []);
  const failures = useAsync(() => api.getOperationsFailures(), []);
  const preparationProgress = useAsync(() => api.getOperationsPreparationProgress(), []);
  // Fetched once on mount, deliberately NOT added to the shared 30s poll
  // below -- a diagnostic aggregate over recent telemetry doesn't need
  // live-pipeline freshness, and this page's own poll cadence is already
  // tuned against a real DB-connection-pool-starvation risk (see
  // POLL_INTERVAL_MS's own comment); adding a 5th recurring call would
  // grow the exact burst that was tuned down, for no real benefit here.
  const quoteDiagnostics = useAsync(() => api.getQuoteDiagnosticsSummary(), []);

  usePolling([
    summary.reload,
    events.reload,
    jobs.reload,
    failures.reload,
    preparationProgress.reload,
  ]);

  if (summary.loading && !summary.data) return <LoadingState label="Loading operations monitor…" />;
  if (summary.error && !summary.data) return <ErrorState message={summary.error} />;
  if (!summary.data) return null;

  const now = summary.data.market_clock.utc_now;

  return (
    <div>
      <div className="page-header">
        <h1>Live Operations</h1>
        <p>
          Real-time visibility into the live forward-test pipeline — every value here is real,
          already-persisted state, refreshed automatically every 30 seconds.
        </p>
      </div>
      <h2 className="sidebar-nav-heading" style={{ marginTop: 16 }}>Control / Official — V3</h2>


      <PreflightBanner preflight={summary.data.preflight} />
      <CriticalAlertBanner
        health={summary.data.health}
        failures={failures.data?.failures ?? []}
        now={now}
      />

      <MarketClockRow clock={summary.data.market_clock} />
      <SystemHealthSection health={summary.data.health} />
      <TodaysOfficialRunCard run={summary.data.official_run} />
      <ExecutionSummaryCard summary={summary.data.execution_summary} />

      {events.data && <TodaysPipeline events={events.data.events} now={now} />}
      {preparationProgress.data &&
        (preparationProgress.data.worker_active ||
          preparationProgress.data.queue_depth > 0 ||
          preparationProgress.data.completed > 0 ||
          preparationProgress.data.failed > 0) && (
          <PreparationProgressCard progress={preparationProgress.data} />
        )}
      {jobs.data && <SchedulerJobsSection jobs={jobs.data.jobs} now={now} />}
      {failures.data && <FailureCenter failures={failures.data.failures} />}
      {quoteDiagnostics.data && <QuoteDiagnosticsSummaryCard summary={quoteDiagnostics.data} />}
      <h2 className="sidebar-nav-heading" style={{ marginTop: 16 }}>Experimental Forward — V4</h2>
      <OperationsV4Section registeredJobCount={summary.data?.health.scheduler.registered_job_count ?? null} />
    </div>
  );
}
