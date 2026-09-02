import type {
  DecisionLifecycleStage,
  DecisionSnapshot,
  EntryCaptureAttempt,
  SettlementCaptureAttempt,
} from "../types/api";

/** True when a decision has no recommended strategy at all -- the real
 * Aug 25 SJM shape (services/decision_snapshot_freezing.py freezes
 * strategy_type=None, legs=None when the engine's own generate_decision
 * genuinely found nothing actionable). Distinct from a real capture
 * failure: capture_benchmark_entry still records a FAILED
 * EntryCaptureAttempt for this case ("no recommended strategy legs to
 * enter"), which must never be shown or counted the same as an
 * infrastructure failure. */
export function isNoActionDecision(decision: Pick<DecisionSnapshot, "legs"> | null): boolean {
  return !decision || !decision.legs || decision.legs.length === 0;
}

/** Mirrors backend services/decision_lifecycle.py::decision_lifecycle_stage
 * plus services/operations.py::derive_lifecycle_state's post-live
 * correction (2026-08-25) -- computed from the same real, already-fetched
 * rows rather than trusting DecisionSnapshot.status, which is frozen at
 * PENDING_ENTRY forever by design (Phase 4.3: the row is fully immutable,
 * including that column) and never reflects the real, derived lifecycle.
 * ``decision`` is optional only for call sites that don't have it loaded
 * yet -- passing it in is what lets this distinguish a genuine no-action
 * decision from a real entry-capture failure; omitting it falls back to
 * the old, coarser pending/entered/settled-only behavior. */
export function deriveLifecycleStage(
  entries: EntryCaptureAttempt[] | undefined,
  settlements: SettlementCaptureAttempt[] | undefined,
  decision?: Pick<DecisionSnapshot, "legs"> | null
): DecisionLifecycleStage {
  if ((settlements ?? []).some((s) => s.status === "captured")) return "settled";
  if ((entries ?? []).some((e) => e.status === "captured")) return "entered";
  if (decision !== undefined && isNoActionDecision(decision)) return "no_action";
  if ((entries ?? []).some((e) => e.status === "failed")) return "entry_failed";
  return "pending_entry";
}

export const LIFECYCLE_LABELS: Record<DecisionLifecycleStage, string> = {
  pending_entry: "Pending Entry",
  entered: "Entered",
  entry_failed: "Entry Failed",
  no_action: "No Action",
  settled: "Settled",
};

/** Shared pill color mapping -- every page that renders a lifecycle
 * stage badge uses this same one, so "Entry Failed" always reads as a
 * real failure (negative) and "No Action" always reads as a real,
 * non-error outcome (neutral), never accidentally diverging per page. */
export const LIFECYCLE_PILL_CLASS: Record<DecisionLifecycleStage, string> = {
  settled: "pill-positive",
  entered: "pill-neutral",
  pending_entry: "pill-neutral",
  no_action: "pill-neutral",
  entry_failed: "pill-negative",
};

export const TIMING_LABELS: Record<string, string> = {
  bmo: "Before Open",
  amc: "After Close",
  dmh: "During Hours",
  unknown: "Time TBD",
};

/** Whole calendar days between today and ``dateStr`` (a plain YYYY-MM-DD
 * date, no time component) -- negative once the date has passed. */
export function daysUntil(dateStr: string): number {
  const target = new Date(`${dateStr}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((target.getTime() - today.getTime()) / 86_400_000);
}

export function earningsCountdownLabel(dateStr: string): string {
  const days = daysUntil(dateStr);
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days < 0) return days === -1 ? "Yesterday" : `${Math.abs(days)}d ago`;
  return `In ${days}d`;
}
