import type { PreparationStep, ResearchJob } from "../types/api";

const STEP_LABELS: Record<string, string> = {
  company_identified: "Company identified",
  historical_earnings: "Historical earnings",
  price_history: "Price history",
  earnings_estimates: "Earnings estimates",
  sec_filings: "SEC filings",
  filing_embeddings: "Filing embeddings",
  options_chain: "Options chain",
  earnings_analysis: "Earnings analysis",
};

const STATUS_ICON: Record<string, string> = {
  pending: "○",
  running: "◐",
  done: "✓",
  skipped: "–",
};

/** A `failed` step reads as a hard "✕" only when it actually broke the
 * whole run (job.status === "failed" — always a required step, see
 * REQUIRED_STEPS in models/research_preparation_job.py). The same status
 * on a job that finished `completed_with_warnings` was an optional step,
 * so it reads as a "⚠" warning instead — the workspace is still usable. */
function iconFor(step: PreparationStep, jobStatus: ResearchJob["status"]): string {
  if (step.status === "failed") return jobStatus === "failed" ? "✕" : "⚠";
  return STATUS_ICON[step.status] ?? "○";
}

export function PreparationProgress({
  steps,
  jobStatus,
}: {
  steps: PreparationStep[];
  jobStatus: ResearchJob["status"];
}) {
  return (
    <ul className="progress-list">
      {steps.map((step) => (
        <li
          key={step.step}
          className={`progress-item progress-${step.status === "failed" && jobStatus !== "failed" ? "warning" : step.status}`}
        >
          <span className="progress-icon" aria-hidden="true">
            {iconFor(step, jobStatus)}
          </span>
          <span className="progress-label">{STEP_LABELS[step.step] ?? step.step}</span>
          {step.detail && <span className="progress-detail">{step.detail}</span>}
        </li>
      ))}
    </ul>
  );
}
