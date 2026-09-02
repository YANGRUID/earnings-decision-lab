import type { DecisionSnapshot } from "../types/api";
import {
  HISTORICAL_MOVE_COMPATIBILITY_EXPLANATION,
  describeHistoricalCompatibility,
} from "../lib/historicalCompatibility";

/** Phase 4 decision-communication hardening (2026-08-26), Sections 29-31
 * -- the one real component every page must use for this value instead
 * of a bare "Probability: X%". Compact mode (dense table cells) shows
 * "88.9% (8/9)" with a small warning glyph when the sample is low; full
 * mode (stat cards / detail views) adds an explicit "LOW SAMPLE" badge,
 * never hidden in a tooltip-only affordance. Both modes carry the same
 * explanatory title attribute. */
export function HistoricalCompatibilityValue({
  snapshot,
  compact = false,
}: {
  snapshot: Pick<
    DecisionSnapshot,
    "estimated_probability" | "historical_sample_size" | "confidence_interval"
  >;
  compact?: boolean;
}) {
  const display = describeHistoricalCompatibility(snapshot);
  if (!display.hasValue) {
    return <span className="text-faint">—</span>;
  }

  if (compact) {
    return (
      <span title={HISTORICAL_MOVE_COMPATIBILITY_EXPLANATION}>
        {display.valueLabel}
        {display.sampleLabel && (
          <span className="text-faint"> ({display.sampleLabel})</span>
        )}
        {display.isLowSample && (
          <span className="pill pill-warning" style={{ marginLeft: 4, padding: "0 4px" }}>
            LOW N
          </span>
        )}
      </span>
    );
  }

  return (
    <span title={HISTORICAL_MOVE_COMPATIBILITY_EXPLANATION}>
      {display.valueLabel}
      {display.sampleLabel && (
        <span className="text-faint"> ({display.sampleLabel} observations)</span>
      )}
      {display.isLowSample && (
        <div className="pill pill-warning" style={{ marginTop: 4, display: "inline-block" }}>
          LOW SAMPLE — NOT A CALIBRATED PROFIT PROBABILITY
        </div>
      )}
    </span>
  );
}
