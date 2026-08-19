import { useEffect, useState } from "react";
import { api, ApiError } from "../../api/client";
import { useAsync } from "../../hooks/useAsync";
import { ErrorState, LoadingState } from "../StatusStates";
import { Markdown } from "../Markdown";
import { formatMoney, formatPlainPercent, providerLabel } from "../../lib/format";
import type {
  AIDecisionVersion,
  DecisionDirection,
  DecisionVolatilityView,
  EstimatedProbability,
  ExpirationCandidate,
  MoveCompatibility,
  RiskProfile,
  ScoredStrategy,
} from "../../types/api";

const DIRECTION_LABELS: Record<DecisionDirection, string> = {
  strong_bullish: "Strongly Bullish",
  bullish: "Bullish",
  neutral: "Neutral",
  bearish: "Bearish",
  strong_bearish: "Strongly Bearish",
};

const VOLATILITY_LABELS: Record<DecisionVolatilityView, string> = {
  long_vol: "Long Volatility",
  neutral_vol: "Neutral Volatility",
  short_vol: "Short Volatility",
};

const RISK_PROFILE_LABELS: Record<RiskProfile, string> = {
  conservative: "Conservative",
  moderate: "Moderate",
  aggressive: "Aggressive",
};

const RISK_PROFILE_DESCRIPTIONS: Record<RiskProfile, string> = {
  conservative:
    "Defined-risk structures only, high quote quality required, lower risk-budget utilization.",
  moderate: "Defined-risk spreads and condors/butterflies, balanced liquidity threshold.",
  aggressive:
    "May allow single-leg long calls/puts and higher risk utilization — never uncovered shorts by default.",
};

// Strategy Ranking V2 (Options Decision Engine V3 Part F) — 9 components
// summing to 100, see analytics/decision/strategy_scoring.py.
const SCORE_COMPONENT_LABELS: Record<string, { label: string; max: number }> = {
  direction_fit: { label: "Direction Fit", max: 15 },
  volatility_fit: { label: "Volatility Fit", max: 11 },
  expiration_fit: { label: "Expiration Fit", max: 10 },
  breakeven_fit: { label: "Breakeven Fit", max: 12 },
  historical_fit: { label: "Historical Fit", max: 12 },
  risk_reward: { label: "Risk/Reward", max: 12 },
  risk_profile_fit: { label: "Risk Profile Fit", max: 8 },
  liquidity: { label: "Liquidity", max: 10 },
  data_quality: { label: "Data Quality", max: 10 },
};

const CONFIDENCE_COMPONENT_LABELS: Record<string, string> = {
  evidence_coverage: "Evidence coverage",
  consensus_agreement: "Consensus agreement",
  historical_consistency: "Historical consistency",
  data_freshness: "Data freshness",
  options_completeness: "Options pricing completeness",
};

function categoryLabel(category: string): string {
  return category.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatVersionLabel(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Reconstructs a display-shaped budget fit for the *recommended* strategy
 * from AIDecisionVersion's flattened top-level columns (trade_budget,
 * recommended_quantity, recommended_capital_at_risk) -- the persisted row
 * doesn't duplicate the full BudgetFit blob for the recommended candidate
 * the way alternative_strategies does per-item, so this derives the same
 * shape from real, already-persisted numbers, never estimating anything
 * the row doesn't actually have. */
function recommendedBudgetFit(decision: AIDecisionVersion): ScoredStrategy["budget_fit"] {
  if (decision.trade_budget === null) return undefined as unknown as ScoredStrategy["budget_fit"];
  if (decision.recommended_quantity === null || decision.recommended_capital_at_risk === null) {
    return null;
  }
  const budget = Number(decision.trade_budget);
  const capitalAtRisk = Number(decision.recommended_capital_at_risk);
  const analysis = decision.recommended_strategy_analysis;
  const qty = decision.recommended_quantity;
  const perContractPremium = analysis ? Number(analysis.net_premium) : null;
  const perContractMaxProfit = analysis?.max_profit !== null && analysis?.max_profit !== undefined
    ? Number(analysis.max_profit)
    : null;
  return {
    trade_budget: decision.trade_budget,
    risk_cap: decision.risk_cap,
    usable_risk_budget: decision.risk_cap ?? decision.trade_budget,
    capital_at_risk_per_contract: qty > 0 ? String(capitalAtRisk / qty) : null,
    max_feasible_quantity: qty,
    total_max_loss: decision.recommended_capital_at_risk,
    total_max_profit:
      perContractMaxProfit !== null ? String(perContractMaxProfit * 100 * qty) : null,
    total_net_premium: perContractPremium !== null ? String(perContractPremium * 100 * qty) : null,
    budget_utilization_pct: budget > 0 ? String((capitalAtRisk / budget) * 100) : null,
    remaining_budget: String(budget - capitalAtRisk),
    feasible: true,
    minimum_required: null,
  };
}

function BudgetFitCard({
  fit,
  legs,
}: {
  fit: ScoredStrategy["budget_fit"];
  legs: ScoredStrategy["legs"];
}) {
  if (fit === null) return null;
  if (!fit.feasible) {
    return (
      <div className="notice" style={{ marginTop: 12, marginBottom: 0 }}>
        <strong>Not feasible for {formatMoney(fit.trade_budget, 0)} budget.</strong>{" "}
        {fit.minimum_required
          ? `Minimum defined risk is ${formatMoney(fit.minimum_required, 0)} per contract.`
          : "This structure has no computable defined risk."}
      </div>
    );
  }
  const contractsPerStructure = legs.reduce((sum, leg) => sum + leg.quantity, 0);
  return (
    <div className="grid grid-4" style={{ gap: 10, marginTop: 12 }}>
      <div className="stat">
        <span className="stat-label">Trade budget</span>
        <span className="stat-value small">{formatMoney(fit.trade_budget, 0)}</span>
      </div>
      <div className="stat">
        <span className="stat-label">Structures</span>
        <span className="stat-value small">{fit.max_feasible_quantity}</span>
      </div>
      <div className="stat">
        <span className="stat-label">Total option contracts</span>
        <span className="stat-value small">{fit.max_feasible_quantity * contractsPerStructure}</span>
      </div>
      <div className="stat">
        <span className="stat-label">Estimated entry</span>
        <span className="stat-value small">
          {fit.total_net_premium !== null
            ? `${formatMoney(fit.total_net_premium, 0)} ${
                Number(fit.total_net_premium) < 0 ? "credit" : "debit"
              }`
            : "—"}
        </span>
      </div>
      <div className="stat">
        <span className="stat-label">Max loss (position)</span>
        <span className="stat-value small">
          {fit.total_max_loss !== null ? formatMoney(fit.total_max_loss, 0) : "—"}
        </span>
      </div>
      <div className="stat">
        <span className="stat-label">Max profit (position)</span>
        <span className="stat-value small">
          {fit.total_max_profit !== null ? formatMoney(fit.total_max_profit, 0) : "Unbounded"}
        </span>
      </div>
      <div className="stat">
        <span className="stat-label">Budget utilization</span>
        <span className="stat-value small">
          {fit.budget_utilization_pct !== null
            ? `${Number(fit.budget_utilization_pct).toFixed(1)}%`
            : "—"}
        </span>
      </div>
      <div className="stat">
        <span className="stat-label">Remaining budget</span>
        <span className="stat-value small">
          {fit.remaining_budget !== null ? formatMoney(fit.remaining_budget, 0) : "—"}
        </span>
      </div>
      {fit.risk_cap !== null && (
        <div className="stat">
          <span className="stat-label">Risk cap</span>
          <span className="stat-value small">{formatMoney(fit.risk_cap, 0)}</span>
        </div>
      )}
    </div>
  );
}

function ScoreBreakdown({ components, total }: { components: Record<string, number>; total: number }) {
  return (
    <div>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>Model Strategy Score: {total} / 100</div>
      <table className="legs-table">
        <tbody>
          {Object.entries(components).map(([key, value]) => {
            const meta = SCORE_COMPONENT_LABELS[key];
            return (
              <tr key={key}>
                <td>{meta?.label ?? key}</td>
                <td className="mono">
                  {value} / {meta?.max ?? "?"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
        A deterministic fit score, not a probability of profit.
      </p>
    </div>
  );
}

/** Options Decision Engine V3 Part E — three deliberately distinct
 * reliability metrics, never collapsed into one number:
 * - Historical Compatibility: how often past real earnings moves would
 *   have satisfied this exact breakeven condition.
 * - Estimated Probability: the same empirical distribution, but relabeled
 *   with its real sample size and (when computable) a Wilson confidence
 *   interval — an estimate, not a guarantee, and never fake precision.
 * - True Strategy Win Rate: the strictest metric (real settled option
 *   entry/exit P&L) — honestly "Unavailable" today because this project
 *   does not yet capture real point-in-time option entry/exit prices for
 *   any settled decision (see services/track_record.py). */
function ProbabilityCard({
  historicalCompatibility,
  estimatedProbability,
}: {
  historicalCompatibility: MoveCompatibility | null;
  estimatedProbability: EstimatedProbability | null;
}) {
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>Historical Reliability</div>
      <table className="legs-table">
        <tbody>
          <tr>
            <td>Historical Compatibility</td>
            <td className="mono">
              {historicalCompatibility
                ? `${formatPlainPercent(historicalCompatibility.compatible_pct)} (${
                    historicalCompatibility.compatible_count
                  }/${historicalCompatibility.sample_size} events)`
                : "Not available"}
            </td>
          </tr>
          <tr>
            <td>Estimated Probability</td>
            <td className="mono">
              {estimatedProbability
                ? `${formatPlainPercent(estimatedProbability.probability)} (Empirical earnings ` +
                  `distribution${estimatedProbability.low_sample_confidence ? ", Low sample confidence" : ""})` +
                  (estimatedProbability.wilson_lower !== null && estimatedProbability.wilson_upper !== null
                    ? ` — 95% CI [${formatPlainPercent(estimatedProbability.wilson_lower)}, ${formatPlainPercent(
                        estimatedProbability.wilson_upper
                      )}]`
                    : "")
                : "Not available"}
            </td>
          </tr>
          <tr>
            <td>True Strategy Win Rate</td>
            <td className="mono">Unavailable (No settled historical option trades yet)</td>
          </tr>
        </tbody>
      </table>
      <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
        These are three different, deliberately unmixed questions: how often the underlying's
        historical move would have satisfied this breakeven (Historical Compatibility / Estimated
        Probability, same empirical basis), versus whether the actual options position was ever
        recorded to make money (True Strategy Win Rate — not yet possible to compute honestly).
      </p>
    </div>
  );
}

function StrategyDecisionCard({
  label,
  strategy,
  historicalCompatibility,
  estimatedProbability,
}: {
  label: string;
  strategy: {
    category: string;
    legs: ScoredStrategy["legs"];
    analysis: ScoredStrategy["analysis"];
    score: number;
    score_components: Record<string, number>;
    why: string[];
    risks: string[];
    budget_fit?: ScoredStrategy["budget_fit"];
    why_expiration?: string[];
    why_strikes?: string[];
    why_risk_profile?: string[];
    why_not_alternative?: string[];
  };
  historicalCompatibility?: MoveCompatibility | null;
  estimatedProbability?: EstimatedProbability | null;
}) {
  const a = strategy.analysis;
  return (
    <div className="card strategy-card">
      <div className="strategy-card-header">
        <span className="strategy-rank">{label}</span>
        <h3>{categoryLabel(strategy.category)}</h3>
      </div>

      <table className="legs-table">
        <thead>
          <tr>
            <th>Action</th>
            <th>Qty</th>
            <th>Type</th>
            <th>Strike</th>
            <th>Premium</th>
          </tr>
        </thead>
        <tbody>
          {strategy.legs.map((leg, i) => (
            <tr key={i}>
              <td className="mono">{leg.action}</td>
              <td className="mono">{leg.quantity}</td>
              <td className="mono">{leg.option_type}</td>
              <td className="mono">{formatMoney(leg.strike)}</td>
              <td className="mono">{formatMoney(leg.premium)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="grid grid-3" style={{ gap: 10, marginTop: 12 }}>
        <div className="stat">
          <span className="stat-label">Net premium</span>
          <span className="stat-value small">{formatMoney(a.net_premium)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Max profit</span>
          <span className="stat-value small">
            {a.max_profit ? formatMoney(a.max_profit) : "Unbounded"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Max loss</span>
          <span className="stat-value small">{a.max_loss ? formatMoney(a.max_loss) : "—"}</span>
        </div>
      </div>
      <div style={{ marginTop: 8, marginBottom: 12 }}>
        <span className="stat-label">Breakeven(s)</span>{" "}
        <span className="mono text-sm">
          {a.breakevens.length ? a.breakevens.map((b) => formatMoney(b)).join(", ") : "—"}
        </span>
      </div>

      <ScoreBreakdown components={strategy.score_components} total={strategy.score} />

      {strategy.budget_fit !== undefined && (
        <BudgetFitCard fit={strategy.budget_fit} legs={strategy.legs} />
      )}

      {(historicalCompatibility !== undefined || estimatedProbability !== undefined) && (
        <ProbabilityCard
          historicalCompatibility={historicalCompatibility ?? null}
          estimatedProbability={estimatedProbability ?? null}
        />
      )}

      <div style={{ marginTop: 12 }}>
        <div style={{ fontWeight: 600 }}>Why this strategy</div>
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {strategy.why.map((line, i) => (
            <li key={i} className="text-sm text-muted">
              {line}
            </li>
          ))}
        </ul>
      </div>

      {strategy.why_expiration && strategy.why_expiration.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontWeight: 600 }}>Why This Expiration</div>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {strategy.why_expiration.map((line, i) => (
              <li key={i} className="text-sm text-muted">
                {line}
              </li>
            ))}
          </ul>
        </div>
      )}

      {strategy.why_strikes && strategy.why_strikes.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontWeight: 600 }}>Why These Strikes</div>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {strategy.why_strikes.map((line, i) => (
              <li key={i} className="text-sm text-muted">
                {line}
              </li>
            ))}
          </ul>
        </div>
      )}

      {strategy.why_risk_profile && strategy.why_risk_profile.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontWeight: 600 }}>Why This Risk Level Fit</div>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {strategy.why_risk_profile.map((line, i) => (
              <li key={i} className="text-sm text-muted">
                {line}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div style={{ marginTop: 10 }}>
        <div style={{ fontWeight: 600 }}>Main risks</div>
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {strategy.risks.map((line, i) => (
            <li key={i} className="text-sm text-muted">
              {line}
            </li>
          ))}
        </ul>
      </div>

      {strategy.why_not_alternative && strategy.why_not_alternative.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontWeight: 600 }}>Why Not #2</div>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {strategy.why_not_alternative.map((line, i) => (
              <li key={i} className="text-sm text-muted">
                {line}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function CurrentViewCard({ decision }: { decision: AIDecisionVersion }) {
  return (
    <div className="card">
      <h2>Current View</h2>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 10 }}>
        <span style={{ fontSize: 20, fontWeight: 600 }}>
          {DIRECTION_LABELS[decision.direction]}
        </span>
        <span className="pill pill-neutral">{VOLATILITY_LABELS[decision.volatility_view]}</span>
        <span className="text-sm text-muted">Confidence {decision.confidence_score} / 100</span>
        {decision.risk_profile && (
          <span className="pill pill-neutral" title={RISK_PROFILE_DESCRIPTIONS[decision.risk_profile]}>
            Risk: {RISK_PROFILE_LABELS[decision.risk_profile]}
          </span>
        )}
        {decision.expiration && (
          <span className="text-sm text-muted">Expiration {decision.expiration}</span>
        )}
        {decision.decision_source === "manual_override" && (
          <span className="pill pill-neutral">Manual override</span>
        )}
      </div>
      <details style={{ marginBottom: 10 }}>
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>Confidence breakdown</summary>
        <table className="legs-table" style={{ marginTop: 8 }}>
          <tbody>
            {Object.entries(decision.confidence_components).map(([key, value]) => (
              <tr key={key}>
                <td>{CONFIDENCE_COMPONENT_LABELS[key] ?? key}</td>
                <td className="mono">{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
          Built entirely from real, already-known signals — never the model's own self-rating.
        </p>
      </details>

      <div className="card" style={{ marginTop: 0 }}>
        <h2>Rationale</h2>
        <Markdown>{decision.rationale}</Markdown>
      </div>
      <div className="grid grid-2" style={{ gap: 16 }}>
        <div className="card">
          <h2>Bull Case</h2>
          <Markdown>{decision.bull_case}</Markdown>
        </div>
        <div className="card">
          <h2>Bear Case</h2>
          <Markdown>{decision.bear_case}</Markdown>
        </div>
      </div>
      <div className="grid grid-2" style={{ gap: 16 }}>
        <div className="card">
          <h2>Key Catalysts</h2>
          <Markdown>{decision.key_catalysts}</Markdown>
        </div>
        <div className="card">
          <h2>Key Risks</h2>
          <Markdown>{decision.key_risks}</Markdown>
        </div>
      </div>

      {decision.implied_move_pct && (
        <p className="text-sm text-muted">
          Options-implied move: ±{formatPlainPercent(decision.implied_move_pct)}. Underlying at
          generation: {decision.underlying_price ? formatMoney(decision.underlying_price) : "—"}.
        </p>
      )}

      {decision.status === "settled" && (
        <div className="notice">
          <strong>Settled.</strong> Actual next-day move:{" "}
          {decision.actual_next_day_move_pct
            ? formatPlainPercent(decision.actual_next_day_move_pct)
            : "—"}
          . Direction:{" "}
          {decision.direction_correct === null
            ? "not gradeable (neutral view)"
            : decision.direction_correct
              ? "Correct"
              : "Incorrect"}
          . Breakeven:{" "}
          {decision.breakeven_met === null
            ? "not available"
            : decision.breakeven_met
              ? "Met"
              : "Not met"}
          . Strategy P&L:{" "}
          {decision.strategy_pnl_available
            ? formatMoney(decision.strategy_pnl)
            : "Not available — no real point-in-time option entry/exit prices were captured."}
        </div>
      )}

      <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
        {decision.disclaimer} Generated {new Date(decision.created_at).toLocaleString()} by{" "}
        {providerLabel(decision.provider)} ({decision.model}).
      </p>
    </div>
  );
}

const DIRECTIONS: DecisionDirection[] = [
  "strong_bullish",
  "bullish",
  "neutral",
  "bearish",
  "strong_bearish",
];
const VOL_VIEWS: DecisionVolatilityView[] = ["long_vol", "neutral_vol", "short_vol"];

const ACTIONABILITY_LABELS: Record<string, string> = {
  actionable_current: "ACTIONABLE — CURRENT",
  actionable_previous_session: "ACTIONABLE — PREVIOUS SESSION",
  stale_research_only: "STALE — RESEARCH ONLY",
  contracts_only: "CONTRACTS ONLY — NO PRICING",
  unavailable: "UNAVAILABLE",
};
const ACTIONABLE_STATUSES = new Set(["actionable_current", "actionable_previous_session"]);

function MarketDataHeader({ ticker }: { ticker: string }) {
  // Reuses Strategy Lab's canonical market-state read (Phase 14.11 Part
  // 30) rather than a second, independently-computed status -- there is
  // exactly one real answer for "is the current options snapshot
  // actionable," and both surfaces show it verbatim.
  const lab = useAsync(() => api.getStrategyLab(ticker), [ticker]);
  if (!lab.data) return null;
  const om = lab.data.options_market;
  const isActionable = ACTIONABLE_STATUSES.has(om.actionability);
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="grid grid-4" style={{ gap: 10 }}>
        <div className="stat">
          <span className="stat-label">Underlying</span>
          <span className="stat-value small">
            {lab.data.underlying_price ? formatMoney(lab.data.underlying_price) : "—"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Options as of</span>
          <span className="stat-value small">
            {om.snapshot_age_label ? `${om.snapshot_age_label} ago` : "—"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Expiration</span>
          <span className="stat-value small">{lab.data.expiration ?? "—"}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Status</span>
          <span className={`pill ${isActionable ? "pill-positive" : "pill-negative"}`}>
            {ACTIONABILITY_LABELS[om.actionability] ?? om.actionability}
          </span>
        </div>
      </div>
      {!isActionable && (
        <div className="notice" style={{ marginTop: 12, marginBottom: 0 }}>
          {om.reason}
        </div>
      )}
      {om.snapshot_purpose === "reconstructed_close" && lab.data.market_session === "regular" && (
        <div className="notice" style={{ marginTop: 12, marginBottom: 0 }}>
          <strong>
            This is not real-time. The market is open, but the current options chain has no
            usable pricing even after a live retry, so this decision is based on a fallback
            rebuilt from the previous session&apos;s close.
          </strong>
          <div className="grid grid-2" style={{ gap: 10, marginTop: 8 }}>
            <div className="stat">
              <span className="stat-label">Current market</span>
              <span className="stat-value small">Open</span>
            </div>
            <div className="stat">
              <span className="stat-label">Current options pricing</span>
              <span className="stat-value small">Unavailable</span>
            </div>
            <div className="stat">
              <span className="stat-label">Fallback pricing</span>
              <span className="stat-value small">Previous-session close</span>
            </div>
            <div className="stat">
              <span className="stat-label">Source</span>
              <span className="stat-value small">IBKR historical reconstruction</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export function DecisionTab({ ticker }: { ticker: string }) {
  const history = useAsync(() => api.getDecisionHistory(ticker), [ticker]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [overrideDirection, setOverrideDirection] = useState<DecisionDirection>("neutral");
  const [overrideVol, setOverrideVol] = useState<DecisionVolatilityView>("neutral_vol");
  const [tradeBudget, setTradeBudget] = useState("");
  const [riskCap, setRiskCap] = useState("");
  const [riskCapIsPercent, setRiskCapIsPercent] = useState(true);
  const [settling, setSettling] = useState(false);
  const [settlementMessage, setSettlementMessage] = useState<string | null>(null);
  const [riskProfile, setRiskProfile] = useState<RiskProfile>("moderate");
  const [expirationMode, setExpirationMode] = useState<"auto" | "manual">("auto");
  const [manualExpiration, setManualExpiration] = useState("");
  const [expirationCandidates, setExpirationCandidates] = useState<ExpirationCandidate[]>([]);
  const [expirationLoading, setExpirationLoading] = useState(false);
  const [expirationWarning, setExpirationWarning] = useState<string | null>(null);

  useEffect(() => {
    if (history.data && history.data.length > 0 && activeId === null) {
      setActiveId(history.data[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history.data]);

  useEffect(() => {
    setSettlementMessage(null);
  }, [activeId]);

  useEffect(() => {
    if (expirationMode !== "manual" || expirationCandidates.length > 0) return;
    let cancelled = false;
    setExpirationLoading(true);
    api
      .getExpirationSelection(ticker, { mode: "auto" })
      .then((result) => {
        if (cancelled) return;
        const all = result.selected
          ? [result.selected, ...result.alternatives]
          : result.alternatives;
        setExpirationCandidates(all);
        setExpirationWarning(result.warning);
        if (all.length > 0 && !manualExpiration) setManualExpiration(all[0].expiration);
      })
      .catch((err) => {
        if (!cancelled) {
          setExpirationWarning(err instanceof ApiError ? err.message : "Could not load real expirations.");
        }
      })
      .finally(() => {
        if (!cancelled) setExpirationLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expirationMode, ticker]);

  const active = history.data?.find((d) => d.id === activeId) ?? null;

  const generate = async (useOverride: boolean) => {
    setGenerating(true);
    setError(null);
    try {
      await api.generateDecision(ticker, {
        ...(useOverride ? { direction: overrideDirection, volatility_view: overrideVol } : {}),
        ...(tradeBudget.trim() ? { trade_budget: tradeBudget.trim() } : {}),
        ...(tradeBudget.trim() && riskCap.trim()
          ? { risk_cap: riskCap.trim(), risk_cap_is_percent: riskCapIsPercent }
          : {}),
        risk_profile: riskProfile,
        ...(expirationMode === "manual" && manualExpiration ? { expiration: manualExpiration } : {}),
      });
      history.reload();
      setActiveId(null);
      setOverrideOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Decision generation failed.");
    } finally {
      setGenerating(false);
    }
  };

  const markFinal = async (id: number) => {
    try {
      await api.markDecisionFinal(ticker, id);
      history.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not mark as final.");
    }
  };

  const trySettle = async (id: number) => {
    setSettling(true);
    setSettlementMessage(null);
    try {
      const result = await api.settleDecision(ticker, id);
      setSettlementMessage(result.message);
      history.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not attempt settlement.");
    } finally {
      setSettling(false);
    }
  };

  if (history.loading && !history.data) return <LoadingState label="Loading decisions…" />;
  if (history.error && !history.data) return <ErrorState message={history.error} />;

  return (
    <div>
      <MarketDataHeader ticker={ticker} />

      <div className="card">
        <p className="text-sm text-muted" style={{ marginTop: 0 }}>
          Connects the AI Earnings Thesis to a direction/volatility view and deterministically
          ranks real strategy candidates against it. The view is an AI judgment call grounded in
          real evidence; every number below it (scores, breakevens, max profit/loss) is computed
          deterministically, never invented. This is not investment advice, and confidence is not
          a probability of a correct outcome. Every generation is saved as a new version — nothing
          is overwritten.
        </p>
        <div className="grid grid-2" style={{ gap: 12, marginBottom: 12, maxWidth: 560 }}>
          <div className="field">
            <label htmlFor="decision-risk-profile">Risk profile</label>
            <select
              id="decision-risk-profile"
              value={riskProfile}
              onChange={(e) => setRiskProfile(e.target.value as RiskProfile)}
              disabled={generating}
            >
              {(Object.keys(RISK_PROFILE_LABELS) as RiskProfile[]).map((p) => (
                <option key={p} value={p}>
                  {RISK_PROFILE_LABELS[p]}
                </option>
              ))}
            </select>
            <span className="text-sm text-faint">{RISK_PROFILE_DESCRIPTIONS[riskProfile]}</span>
          </div>
          <div className="field">
            <label>Expiration</label>
            <select
              value={expirationMode}
              onChange={(e) => setExpirationMode(e.target.value as "auto" | "manual")}
              disabled={generating}
            >
              <option value="auto">Auto (best real expiration)</option>
              <option value="manual">Manual (choose a real expiration)</option>
            </select>
          </div>
        </div>
        {expirationMode === "manual" && (
          <div className="field" style={{ maxWidth: 560, marginBottom: 12 }}>
            <label>Real expiration</label>
            {expirationLoading ? (
              <span className="text-sm text-muted">Loading real listed expirations…</span>
            ) : expirationCandidates.length > 0 ? (
              <select
                value={manualExpiration}
                onChange={(e) => setManualExpiration(e.target.value)}
                disabled={generating}
              >
                {expirationCandidates.map((c) => (
                  <option key={c.expiration} value={c.expiration}>
                    {c.expiration} — {c.dte} DTE, {c.quality}, score {c.score.total}/100
                  </option>
                ))}
              </select>
            ) : (
              <span className="text-sm text-muted">
                {expirationWarning ?? "No real expirations discovered yet."}
              </span>
            )}
            {expirationWarning && expirationCandidates.length > 0 && (
              <span className="text-sm text-faint">{expirationWarning}</span>
            )}
          </div>
        )}
        <div className="grid grid-3" style={{ gap: 12, marginBottom: 12, maxWidth: 560 }}>
          <div className="field">
            <label>Trade budget (optional)</label>
            <input
              type="number"
              min="0"
              placeholder="e.g. 500"
              value={tradeBudget}
              onChange={(e) => setTradeBudget(e.target.value)}
              disabled={generating}
            />
          </div>
          <div className="field">
            <label>Max risk (optional)</label>
            <input
              type="number"
              min="0"
              max={riskCapIsPercent ? 100 : undefined}
              placeholder={riskCapIsPercent ? "e.g. 25" : "e.g. 200"}
              value={riskCap}
              onChange={(e) => setRiskCap(e.target.value)}
              disabled={generating || !tradeBudget.trim()}
            />
            {riskCapIsPercent && (
              <span className="text-sm text-faint">Percent of trade budget, 0–100.</span>
            )}
          </div>
          <div className="field">
            <label>Max risk unit</label>
            <select
              value={riskCapIsPercent ? "percent" : "dollar"}
              onChange={(e) => setRiskCapIsPercent(e.target.value === "percent")}
              disabled={generating || !tradeBudget.trim()}
            >
              <option value="percent">% of budget</option>
              <option value="dollar">$ amount</option>
            </select>
          </div>
        </div>
        {tradeBudget.trim() && (
          <p className="text-sm text-muted" style={{ marginTop: -6, marginBottom: 12 }}>
            Restricts recommended strategies to ones this budget can actually afford, and sizes
            contract quantity by real capital at risk — never just a cosmetic quantity label.
          </p>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button className="btn" onClick={() => generate(false)} disabled={generating}>
            {generating ? "Generating…" : "Generate New Decision"}
          </button>
          <button
            className="btn-secondary"
            onClick={() => setOverrideOpen((v) => !v)}
            disabled={generating}
          >
            {overrideOpen ? "Cancel override" : "Override view manually"}
          </button>
        </div>
        {overrideOpen && (
          <div className="grid grid-2" style={{ gap: 12, marginTop: 12, maxWidth: 480 }}>
            <div className="field">
              <label>Direction</label>
              <select
                value={overrideDirection}
                onChange={(e) => setOverrideDirection(e.target.value as DecisionDirection)}
              >
                {DIRECTIONS.map((d) => (
                  <option key={d} value={d}>
                    {DIRECTION_LABELS[d]}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Volatility view</label>
              <select
                value={overrideVol}
                onChange={(e) => setOverrideVol(e.target.value as DecisionVolatilityView)}
              >
                {VOL_VIEWS.map((v) => (
                  <option key={v} value={v}>
                    {VOLATILITY_LABELS[v]}
                  </option>
                ))}
              </select>
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <button className="btn" onClick={() => generate(true)} disabled={generating}>
                {generating ? "Generating…" : "Generate With This View"}
              </button>
            </div>
          </div>
        )}
        {error && (
          <div className="notice" style={{ marginTop: 12 }}>
            {error}
          </div>
        )}
      </div>

      {history.data && history.data.length > 0 && (
        <div className="card">
          <h2>Decision History</h2>
          <ul className="history-list">
            {history.data.map((d, i) => (
              <li
                key={d.id}
                className={`history-item ${d.id === activeId ? "active" : ""}`}
                onClick={() => setActiveId(d.id)}
              >
                <span className="history-item-question">
                  {i === 0 ? "Latest — " : ""}
                  {formatVersionLabel(d.created_at)} · {DIRECTION_LABELS[d.direction]} · Score{" "}
                  {d.recommended_strategy_score ?? "—"}
                  {d.is_final ? " · Final" : ""}
                  {d.status === "settled" ? " · Settled" : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {active && (
        <>
          <div style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <button
                className="btn-secondary"
                onClick={() => markFinal(active.id)}
                disabled={active.is_final}
              >
                {active.is_final ? "Final Decision" : "Mark as Final Decision"}
              </button>
              {active.settlement_state !== "settled" && (
                <button
                  className="btn-secondary"
                  onClick={() => trySettle(active.id)}
                  disabled={settling}
                >
                  {settling ? "Attempting Settlement…" : "Attempt Settlement"}
                </button>
              )}
              {active.settlement_state !== "settled" && !settlementMessage && (
                <span className="text-sm text-muted">{active.settlement_reason}</span>
              )}
            </div>
            {settlementMessage && (
              <div className="notice" style={{ marginTop: 8, marginBottom: 0 }}>
                {settlementMessage}
              </div>
            )}
          </div>

          <CurrentViewCard decision={active} />

          {(active.recommended_strategy_category || (active.alternative_strategies?.length ?? 0) > 0) && (
            <h2 style={{ marginTop: 20 }}>Recommended Strategies</h2>
          )}

          {active.recommended_strategy_category &&
            active.recommended_strategy_legs &&
            active.recommended_strategy_analysis &&
            active.recommended_strategy_score !== null &&
            active.recommended_strategy_score_components &&
            active.recommended_strategy_why &&
            active.recommended_strategy_risks && (
              <StrategyDecisionCard
                label="#1 Recommended"
                strategy={{
                  category: active.recommended_strategy_category,
                  legs: active.recommended_strategy_legs,
                  analysis: active.recommended_strategy_analysis,
                  score: active.recommended_strategy_score,
                  score_components: active.recommended_strategy_score_components,
                  why: active.recommended_strategy_why,
                  risks: active.recommended_strategy_risks,
                  budget_fit: recommendedBudgetFit(active),
                  why_expiration: active.recommended_strategy_why_expiration ?? undefined,
                  why_strikes: active.recommended_strategy_why_strikes ?? undefined,
                  why_risk_profile: active.recommended_strategy_why_risk_profile ?? undefined,
                  why_not_alternative: active.recommended_strategy_why_not_alternative ?? undefined,
                }}
                historicalCompatibility={active.historical_compatibility}
                estimatedProbability={active.estimated_probability}
              />
            )}

          {active.alternative_strategies?.map((alt, i) => (
            <StrategyDecisionCard key={i} label={`#${i + 2} Alternative`} strategy={alt} />
          ))}

          {!active.recommended_strategy_category && active.no_market_data_reason && (
            <div className="card">
              <h2>Strategy Candidates</h2>
              <div className="notice" style={{ marginBottom: 0 }}>
                <strong>NO ACTIONABLE OPTIONS RECOMMENDATION.</strong> {active.no_market_data_reason}
                {active.trade_budget && (
                  <>
                    {" "}
                    Budget is irrelevant here — no priceable strategy exists to size against, with
                    or without a {formatMoney(active.trade_budget, 0)} budget.
                  </>
                )}
              </div>
            </div>
          )}

          {!active.recommended_strategy_category &&
            !active.no_market_data_reason &&
            active.trade_budget && (
              <div className="card">
                <h2>Strategy Candidates</h2>
                <div className="notice" style={{ marginBottom: 0 }}>
                  <strong>Not feasible for {formatMoney(active.trade_budget, 0)} budget.</strong>{" "}
                  {active.budget_infeasible_minimum
                    ? `Real candidates exist, but the minimum defined risk among them is ${formatMoney(
                        active.budget_infeasible_minimum,
                        0
                      )} per contract -- more than this budget can size even a single contract for.`
                    : "Real candidates exist on this chain, but none fit within this budget."}
                </div>
              </div>
            )}

          {!active.recommended_strategy_category &&
            !active.no_market_data_reason &&
            !active.trade_budget && (
              <div className="card">
                <h2>Strategy Candidates</h2>
                <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
                  No strategy candidates were available at generation time — the real options chain
                  for {ticker} did not have priceable bid/ask/last data on any contract (see the
                  Strategy Lab and Upcoming Earnings tabs for the same real options-chain state).
                  No strategy is fabricated when this happens.
                </p>
              </div>
            )}

          {active.citations.length > 0 && (
            <div className="card">
              <h2>Sources</h2>
              {active.citations.map((c) => (
                <div key={c.marker} className="source-item">
                  <span className="citation-badge">{c.marker}</span>
                  <div>
                    <div className="source-title">
                      {c.ticker} · {c.filing_type} · filed {c.filing_date}
                      {c.section ? ` · ${c.section}` : ""}
                    </div>
                    <a className="text-link text-sm" href={c.source_url} target="_blank" rel="noreferrer">
                      View source
                    </a>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {!history.loading && history.data && history.data.length === 0 && (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            No decisions generated yet for {ticker}.
          </p>
        </div>
      )}
    </div>
  );
}
