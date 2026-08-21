import type {
  DecisionLifecycleStage,
  EntryCaptureAttempt,
  SettlementCaptureAttempt,
} from "../types/api";

/** Mirrors backend services/decision_lifecycle.py::decision_lifecycle_stage
 * exactly (a CAPTURED SettlementCaptureAttempt means settled; else a
 * CAPTURED EntryCaptureAttempt means entered; else pending) -- computed
 * here from the same real, already-fetched rows rather than trusting
 * DecisionSnapshot.status, which is frozen at PENDING_ENTRY forever by
 * design (Phase 4.3: the row is fully immutable, including that column)
 * and never reflects the real, derived lifecycle. */
export function deriveLifecycleStage(
  entries: EntryCaptureAttempt[] | undefined,
  settlements: SettlementCaptureAttempt[] | undefined
): DecisionLifecycleStage {
  if ((settlements ?? []).some((s) => s.status === "captured")) return "settled";
  if ((entries ?? []).some((e) => e.status === "captured")) return "entered";
  return "pending_entry";
}

export const LIFECYCLE_LABELS: Record<DecisionLifecycleStage, string> = {
  pending_entry: "Pending Entry",
  entered: "Entered",
  settled: "Settled",
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
