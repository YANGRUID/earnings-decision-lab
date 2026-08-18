import type { PreparationStep } from "../types/api";

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
  done: "●",
  failed: "✕",
  skipped: "–",
};

export function PreparationProgress({ steps }: { steps: PreparationStep[] }) {
  return (
    <ul className="progress-list">
      {steps.map((step) => (
        <li key={step.step} className={`progress-item progress-${step.status}`}>
          <span className="progress-icon" aria-hidden="true">
            {STATUS_ICON[step.status] ?? "○"}
          </span>
          <span className="progress-label">{STEP_LABELS[step.step] ?? step.step}</span>
          {step.detail && <span className="progress-detail">{step.detail}</span>}
        </li>
      ))}
    </ul>
  );
}
