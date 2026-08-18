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

const SCORE_COMPONENT_LABELS: Record<string, { label: string; max: number }> = {
  direction_fit: { label: "Direction Fit", max: 25 },
  breakeven_fit: { label: "Breakeven Fit", max: 20 },
  historical_fit: { label: "Historical Fit", max: 20 },
  risk_reward: { label: "Risk/Reward", max: 20 },
  liquidity: { label: "Liquidity", max: 10 },
  data_quality: { label: "Data Quality", max: 5 },
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

function StrategyDecisionCard({
  label,
  strategy,
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
  };
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
            <th>Type</th>
            <th>Strike</th>
            <th>Premium</th>
          </tr>
        </thead>
        <tbody>
          {strategy.legs.map((leg, i) => (
            <tr key={i}>
              <td className="mono">{leg.action}</td>
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

export function DecisionTab({ ticker }: { ticker: string }) {
  const history = useAsync(() => api.getDecisionHistory(ticker), [ticker]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [overrideDirection, setOverrideDirection] = useState<DecisionDirection>("neutral");
  const [overrideVol, setOverrideVol] = useState<DecisionVolatilityView>("neutral_vol");

  useEffect(() => {
    if (history.data && history.data.length > 0 && activeId === null) {
      setActiveId(history.data[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history.data]);

  const active = history.data?.find((d) => d.id === activeId) ?? null;

  const generate = async (useOverride: boolean) => {
    setGenerating(true);
    setError(null);
    try {
      await api.generateDecision(
        ticker,
        useOverride ? { direction: overrideDirection, volatility_view: overrideVol } : undefined
      );
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
    try {
      await api.settleDecision(ticker, id);
      history.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not attempt settlement.");
    }
  };

  if (history.loading && !history.data) return <LoadingState label="Loading decisions…" />;
  if (history.error && !history.data) return <ErrorState message={history.error} />;

  return (
    <div>
      <div className="card">
        <p className="text-sm text-muted" style={{ marginTop: 0 }}>
          Connects the AI Earnings Thesis to a direction/volatility view and deterministically
          ranks real strategy candidates against it. The view is an AI judgment call grounded in
          real evidence; every number below it (scores, breakevens, max profit/loss) is computed
          deterministically, never invented. This is not investment advice, and confidence is not
          a probability of a correct outcome. Every generation is saved as a new version — nothing
          is overwritten.
        </p>
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
          <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
            <button
              className="btn-secondary"
              onClick={() => markFinal(active.id)}
              disabled={active.is_final}
            >
              {active.is_final ? "Final Decision" : "Mark as Final Decision"}
            </button>
            {active.status === "open" && (
              <button className="btn-secondary" onClick={() => trySettle(active.id)}>
                Attempt Settlement
              </button>
            )}
          </div>

          <CurrentViewCard decision={active} />

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
                }}
              />
            )}

          {active.alternative_strategies?.map((alt, i) => (
            <StrategyDecisionCard key={i} label={`#${i + 2} Alternative`} strategy={alt} />
          ))}

          {!active.recommended_strategy_category && (
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
