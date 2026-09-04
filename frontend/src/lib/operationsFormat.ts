// Shared formatting for the V4 operations surfaces (Live Operations and
// the Dashboard pipeline). Kept outside the page modules so React Fast
// Refresh sees component-only files.

export const JOB_LABELS: Record<string, string> = {
  earnings_calendar_sync: "Earnings Calendar Sync",
  earnings_research_preparation: "Research Preparation (nightly)",
  research_readiness_catchup: "Research Readiness Catch-up (13:00 ET)",
  research_preparation_startup_catchup: "Research Preparation Startup Catch-up",
  ibkr_gateway_healthcheck: "IBKR Provider Healthcheck",
  v4_forward_window: "V4 Forward Window (15:30 ET: settlements, then decisions)",
  v4_shadow_decision: "V4 Decision phase (15:30 ET)",
  v4_shadow_settlement: "V4 Settlement phase (15:30 ET, T+1)",
  v4_eod_settlement_fallback: "V4 End-of-Day Settlement Fallback (16:30 ET)",
};

// Human labels for the backend's V4 pipeline states (services/operations.py).
export const STATE_LABELS: Record<string, string> = {
  CALENDAR_DISCOVERED: "CALENDAR DISCOVERED",
  BUSINESS_INELIGIBLE: "NOT ELIGIBLE",
  COMPANY_RESOLUTION_FAILED: "COMPANY UNRESOLVED",
  RESEARCH_QUEUED: "RESEARCH QUEUED",
  RESEARCH_RUNNING: "RESEARCH RUNNING",
  RESEARCH_READY: "READY FOR V4 DECISION",
  RESEARCH_FAILED: "RESEARCH FAILED",
  RESEARCH_NOT_READY: "RESEARCH NOT READY",
  WAITING_DECISION: "WAITING DECISION",
  DECISION_WINDOW_MISSED: "DECISION WINDOW MISSED",
  DECISION_FAILED: "DECISION FAILED",
  DEADLINE_SKIPPED: "DEADLINE SKIPPED",
  NO_ACTION: "NO ACTION",
  ENTRY_OBSERVED: "ENTRY OBSERVED",
  ENTRY_FAILED: "ENTRY FAILED",
  WAITING_SETTLEMENT: "WAITING SETTLEMENT",
  SETTLED: "SETTLED",
  SETTLEMENT_FAILED: "SETTLEMENT FAILED",
};

export function stateLabel(state: string): string {
  return STATE_LABELS[state] ?? state.replace(/_/g, " ");
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function countdown(targetIso: string | null, nowIso: string): string {
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

/** A timestamp rendered in Eastern time, the clock every V4 window is defined in. */
export function formatEt(iso: string | null): string {
  if (!iso) return "—";
  return `${new Date(iso).toLocaleString("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })} ET`;
}

/** Human labels for failure-centre stage codes (the raw code stays in the table). */
export const STAGE_LABELS: Record<string, string> = {
  research_gate: "research not ready at the decision window",
  deadline_guard: "deadline reached before evaluation",
  settlement: "settlement observation",
  research_preparation: "research preparation",
  view: "DecisionView generation",
  candidates: "candidate assembly",
};
