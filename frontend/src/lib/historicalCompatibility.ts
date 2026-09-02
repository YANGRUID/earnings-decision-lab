/** Phase 4 decision-communication hardening (2026-08-26), Sections 29-31.
 *
 * DecisionSnapshot.estimated_probability is NOT probability of profit,
 * option delta probability, backtest win rate, market-implied
 * probability, LLM confidence, or a calibrated forecast probability --
 * it is MoveCompatibility.compatible_pct (analytics/options/move_
 * compatibility.py): the fraction of this company's own real historical
 * earnings moves that would have satisfied this exact candidate's
 * breakeven condition, wrapped with a 95% Wilson CI and a low-sample
 * flag (see services/decision_snapshot_freezing.py's own
 * FrozenProbability). Every real caller must use this label, this
 * sample-size context, and this low-sample warning -- never a bare
 * percentage under the word "Probability."
 */

import type { DecisionSnapshot } from "../types/api";
import { formatPlainPercent } from "./format";

export const HISTORICAL_MOVE_COMPATIBILITY_LABEL = "Historical Move Compatibility";

export const HISTORICAL_MOVE_COMPATIBILITY_EXPLANATION =
  "The fraction of this company's own real historical earnings moves that would have " +
  "satisfied this strategy's breakeven condition. Not probability of profit, not a " +
  "calibrated forecast, not an options-market-implied probability.";

// Defensive fallback only -- the real source of truth is the backend's
// own confidence_interval.low_sample_confidence flag (analytics/
// decision/probability.py's LOW_SAMPLE_THRESHOLD = 20), computed once at
// freeze time. Used only if that flag is ever missing on an otherwise-
// valued row, never as a second, independently-drifting threshold.
export const LOW_SAMPLE_THRESHOLD = 20;

export interface HistoricalCompatibilityDisplay {
  /** "88.9%" or "—" when no real value exists. */
  valueLabel: string;
  /** "8/9" when a real sample size is known, else null. */
  sampleLabel: string | null;
  isLowSample: boolean;
  hasValue: boolean;
}

export function describeHistoricalCompatibility(
  snapshot: Pick<
    DecisionSnapshot,
    "estimated_probability" | "historical_sample_size" | "confidence_interval"
  >
): HistoricalCompatibilityDisplay {
  const sampleSize = snapshot.historical_sample_size;
  const lowSampleFlag = snapshot.confidence_interval?.low_sample_confidence ?? false;
  return {
    valueLabel: snapshot.estimated_probability
      ? formatPlainPercent(snapshot.estimated_probability, 1)
      : "—",
    sampleLabel: sampleSize !== null ? `${sampleSize}` : null,
    isLowSample: lowSampleFlag || (sampleSize !== null && sampleSize < LOW_SAMPLE_THRESHOLD),
    hasValue: snapshot.estimated_probability !== null,
  };
}
