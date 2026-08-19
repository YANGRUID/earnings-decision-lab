import { useState } from "react";
import { api, ApiError } from "../../api/client";
import { useAsync } from "../../hooks/useAsync";
import { ErrorState, LoadingState } from "../StatusStates";
import {
  dataStateLabel,
  formatMoney,
  formatPercent,
  MARKET_SESSION_LABELS,
  providerLabel,
} from "../../lib/format";
import type {
  OptionLegInput,
  OptionQuote,
  RankedStrategy,
  StrategyLab,
  StrategyPayoffResponse,
} from "../../types/api";

const STALE_DATA_STATES = new Set(["stale", "previous_session"]);

const ET_TIME_FORMAT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function formatEtTimestamp(iso: string | null): string | null {
  if (!iso) return null;
  return `${ET_TIME_FORMAT.format(new Date(iso))} ET`;
}

const EARNINGS_ANCHOR_LABELS: Record<string, string> = {
  confirmed: "Confirmed by provider",
  estimated: "Estimated",
  manual: "Manual override",
  unknown: "Unknown",
};

const ACTIONABILITY_LABELS: Record<string, string> = {
  actionable_current: "ACTIONABLE — CURRENT",
  actionable_previous_session: "ACTIONABLE — PREVIOUS SESSION",
  stale_research_only: "STALE — RESEARCH ONLY",
  contracts_only: "CONTRACTS ONLY — NO PRICING",
  unavailable: "UNAVAILABLE",
};

const ACTIONABLE_STATUSES = new Set(["actionable_current", "actionable_previous_session"]);

function StrategyLabStateBar({ lab }: { lab: StrategyLab }) {
  const isStale = STALE_DATA_STATES.has(lab.data_state);
  const actionability = lab.options_market.actionability;
  const isActionable = ACTIONABLE_STATUSES.has(actionability);
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="grid grid-3" style={{ gap: 10 }}>
        <div className="stat">
          <span className="stat-label">Market</span>
          <span className="stat-value small">
            {MARKET_SESSION_LABELS[lab.market_session] ?? lab.market_session}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Options source</span>
          <span className="stat-value small">
            {lab.snapshot_source ? providerLabel(lab.snapshot_source) : "Not collected"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Snapshot</span>
          <span className="stat-value small">
            {lab.snapshot_age_label ? `${lab.snapshot_age_label} ago` : "—"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Data quality</span>
          <span className={`pill ${isStale ? "pill-negative" : "pill-positive"}`}>
            {dataStateLabel(lab.data_state)}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Earnings anchor</span>
          <span className="stat-value small">
            {EARNINGS_ANCHOR_LABELS[lab.earnings_anchor_status] ?? lab.earnings_anchor_status}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Status</span>
          <span className={`pill ${isActionable ? "pill-positive" : "pill-negative"}`}>
            {ACTIONABILITY_LABELS[actionability] ?? actionability}
          </span>
        </div>
      </div>
      {lab.options_market.chain_exists && (
        <div className="text-sm text-faint" style={{ marginTop: 10 }}>
          Chain quality: {lab.options_market.contract_count} contracts ·{" "}
          {lab.options_market.bid_ask_contract_count} with bid/ask ·{" "}
          {lab.options_market.iv_contract_count} with IV · {lab.options_market.greeks_contract_count}{" "}
          with Greeks · volume on {formatPercent(lab.options_market.volume_coverage, 0)} · open
          interest on {formatPercent(lab.options_market.oi_coverage, 0)}
        </div>
      )}
      {lab.options_market.snapshot_purpose === "reconstructed_close" && (
        <div className="notice" style={{ marginTop: 12, marginBottom: 0 }}>
          <strong>Options: reconstructed from IBKR historical market data.</strong> The current
          chain had no priceable quotes, so the previous session's close was rebuilt from real
          IBKR historical bars (last-price only -- no historical bid/ask is exposed by this
          endpoint) rather than reusing an old intraday snapshot.
          {lab.snapshot_age_label && ` Reconstructed close is ${lab.snapshot_age_label} old.`}
          <div className="grid grid-2" style={{ gap: 10, marginTop: 8 }}>
            <div className="stat">
              <span className="stat-label">Underlying as of</span>
              <span className="stat-value small mono">
                {formatEtTimestamp(lab.options_market.underlying_timestamp) ?? "—"}
                {lab.underlying_price ? ` · ${formatMoney(lab.underlying_price)}` : ""}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">Options as of</span>
              <span className="stat-value small mono">
                {formatEtTimestamp(lab.snapshot_timestamp) ?? "—"}
              </span>
            </div>
          </div>
        </div>
      )}
      {isStale && lab.options_market.snapshot_purpose !== "reconstructed_close" && (
        <div className="notice" style={{ marginTop: 12, marginBottom: 0 }}>
          <strong>
            Using {lab.snapshot_source ? providerLabel(lab.snapshot_source) : "a"}{" "}
            {lab.options_market.is_fallback_snapshot
              ? lab.options_market.snapshot_purpose === "close"
                ? "previous close"
                : "previous-session"
              : "previous session"}{" "}
            snapshot
          </strong>
          {lab.snapshot_age_label && ` (${lab.snapshot_age_label} ago)`}
          {lab.options_market.is_fallback_snapshot
            ? " — the current chain has no priceable quotes, so pricing was carried forward " +
              "from this earlier snapshot instead of leaving the whole page unavailable."
            : " — every strategy below is computed from this stale data and may differ " +
              "materially from current tradable prices."}
        </div>
      )}
    </div>
  );
}

function pct(value: string | null, digits = 1): string {
  if (value === null) return "—";
  return formatPercent(Number(value), digits);
}

function categoryLabel(category: string): string {
  return category.replace(/_/g, " ");
}

function StrategyCard({ strategy, ticker }: { strategy: RankedStrategy; ticker: string }) {
  const a = strategy.analysis;
  return (
    <div className="card strategy-card">
      <div className="strategy-card-header">
        <span className="strategy-rank">#{strategy.rank}</span>
        <h3>{categoryLabel(strategy.category)}</h3>
      </div>

      <table className="legs-table">
        <thead>
          <tr>
            <th>Action</th>
            <th>Qty</th>
            <th>Type</th>
            <th>Strike</th>
            <th>Est. entry (mid)</th>
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
      <p className="text-sm text-faint" style={{ marginTop: 4, marginBottom: 0 }}>
        Premiums are estimated at the bid/ask midpoint ((bid+ask)/2) as of the snapshot above —
        never an execution price. See the real option chain below for each contract's actual
        bid/ask and spread.
      </p>

      <div className="grid grid-3" style={{ gap: 10, marginTop: 12 }}>
        <div className="stat">
          <span className="stat-label">Net premium</span>
          <span className="stat-value small">{formatMoney(a.net_premium)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Max profit</span>
          <span className="stat-value small">{a.max_profit ? formatMoney(a.max_profit) : "Unbounded"}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Max loss</span>
          <span className="stat-value small">{a.max_loss ? formatMoney(a.max_loss) : "—"}</span>
        </div>
      </div>
      <div style={{ marginTop: 8 }}>
        <span className="stat-label">Breakeven(s)</span>{" "}
        <span className="mono text-sm">
          {a.breakevens.length ? a.breakevens.map((b) => formatMoney(b)).join(", ") : "—"}
        </span>
      </div>

      <p className="text-sm text-muted" style={{ marginTop: 10 }}>
        {strategy.explanation}
      </p>

      {strategy.move_compatibility && (
        <p className="text-sm text-muted" style={{ marginTop: 6, marginBottom: 0 }}>
          <strong>Historical move compatibility</strong> (not a backtest — real past {ticker}{" "}
          earnings-day moves only): {strategy.move_compatibility.compatible_count} of{" "}
          {strategy.move_compatibility.sample_size} real past moves were{" "}
          {strategy.move_compatibility.requires_move_beyond_threshold ? "at least" : "less than"}{" "}
          {pct(strategy.move_compatibility.required_move_pct)} (
          {pct(strategy.move_compatibility.compatible_pct, 0)} of history).
        </p>
      )}

    </div>
  );
}

function OptionChainTable({ chain }: { chain: OptionQuote[] }) {
  const byExpiration = new Map<string, OptionQuote[]>();
  for (const q of chain) {
    const list = byExpiration.get(q.expiration_date) ?? [];
    list.push(q);
    byExpiration.set(q.expiration_date, list);
  }

  return (
    <div className="card">
      <h2>Real option chain</h2>
      {[...byExpiration.entries()].map(([expiration, quotes]) => (
        <div key={expiration} style={{ marginBottom: 16 }}>
          <div className="text-sm text-muted mono" style={{ marginBottom: 6 }}>
            Expiration {expiration}
          </div>
          <table>
            <thead>
              <tr>
                <th>Type</th>
                <th>Strike</th>
                <th>Bid</th>
                <th>Ask</th>
                <th>Spread</th>
                <th>IV</th>
                <th>Delta</th>
                <th>OI</th>
                <th>Quality</th>
              </tr>
            </thead>
            <tbody>
              {quotes
                .sort((x, y) => Number(x.strike) - Number(y.strike))
                .map((q, i) => {
                  const bid = q.bid !== null ? Number(q.bid) : null;
                  const ask = q.ask !== null ? Number(q.ask) : null;
                  const mid = bid !== null && ask !== null ? (bid + ask) / 2 : null;
                  const spreadAbs = bid !== null && ask !== null ? ask - bid : null;
                  const spreadPct = spreadAbs !== null && mid && mid > 0 ? spreadAbs / mid : null;
                  return (
                    <tr key={i}>
                      <td className="mono">{q.option_type}</td>
                      <td className="mono">{formatMoney(q.strike)}</td>
                      <td className="mono">{q.bid ? formatMoney(q.bid) : "—"}</td>
                      <td className="mono">{q.ask ? formatMoney(q.ask) : "—"}</td>
                      <td className="mono">
                        {spreadAbs !== null && spreadPct !== null
                          ? `${spreadAbs.toFixed(2)} (${formatPercent(spreadPct, 0)})`
                          : "—"}
                      </td>
                      <td className="mono">
                        {q.implied_volatility ? pct(q.implied_volatility) : "—"}
                      </td>
                      <td className="mono">{q.delta ?? "—"}</td>
                      <td className="mono">{q.open_interest ?? "—"}</td>
                      <td>
                        {q.market_data_quality ? (
                          <span className="pill pill-neutral">{q.market_data_quality}</span>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

const MANUAL_PRESETS: Record<string, OptionLegInput[]> = {
  "Long call": [{ option_type: "call", action: "buy", strike: "100", premium: "5" }],
  "Long put": [{ option_type: "put", action: "buy", strike: "100", premium: "4" }],
  "Bull call spread": [
    { option_type: "call", action: "buy", strike: "100", premium: "6" },
    { option_type: "call", action: "sell", strike: "110", premium: "2" },
  ],
  "Iron condor": [
    { option_type: "put", action: "buy", strike: "90", premium: "1" },
    { option_type: "put", action: "sell", strike: "95", premium: "2" },
    { option_type: "call", action: "sell", strike: "105", premium: "2" },
    { option_type: "call", action: "buy", strike: "110", premium: "1" },
  ],
};

function ManualStrategyCalculator() {
  const [label, setLabel] = useState("Bull call spread");
  const [legs, setLegs] = useState<OptionLegInput[]>(MANUAL_PRESETS["Bull call spread"]);
  const [result, setResult] = useState<StrategyPayoffResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const applyPreset = (name: string) => {
    setLabel(name);
    setLegs(MANUAL_PRESETS[name].map((l) => ({ ...l })));
    setResult(null);
  };

  const updateLeg = (index: number, patch: Partial<OptionLegInput>) => {
    setLegs((prev) => prev.map((leg, i) => (i === index ? { ...leg, ...patch } : leg)));
  };

  const submit = async () => {
    setError(null);
    try {
      setResult(await api.calculatePayoff({ strategy_label: label, legs }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Calculation failed.");
    }
  };

  return (
    <details className="card">
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>
        Advanced: manual strategy calculator (your own strikes/premiums)
      </summary>
      <div style={{ marginTop: 14 }}>
        <div className="field">
          <label>Preset</label>
          <select value={label} onChange={(e) => applyPreset(e.target.value)}>
            {Object.keys(MANUAL_PRESETS).map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </div>
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Action</th>
              <th>Strike</th>
              <th>Premium</th>
              <th>Qty</th>
            </tr>
          </thead>
          <tbody>
            {legs.map((leg, i) => (
              <tr key={i}>
                <td>
                  <select
                    value={leg.option_type}
                    onChange={(e) => updateLeg(i, { option_type: e.target.value as "call" | "put" })}
                  >
                    <option value="call">Call</option>
                    <option value="put">Put</option>
                  </select>
                </td>
                <td>
                  <select
                    value={leg.action}
                    onChange={(e) => updateLeg(i, { action: e.target.value as "buy" | "sell" })}
                  >
                    <option value="buy">Buy</option>
                    <option value="sell">Sell</option>
                  </select>
                </td>
                <td>
                  <input
                    type="number"
                    value={leg.strike}
                    onChange={(e) => updateLeg(i, { strike: e.target.value })}
                    style={{ width: 80 }}
                  />
                </td>
                <td>
                  <input
                    type="number"
                    value={leg.premium}
                    onChange={(e) => updateLeg(i, { premium: e.target.value })}
                    style={{ width: 80 }}
                  />
                </td>
                <td>
                  <input
                    type="number"
                    value={leg.quantity ?? 1}
                    onChange={(e) => updateLeg(i, { quantity: Number(e.target.value) })}
                    style={{ width: 60 }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button className="btn" style={{ marginTop: 10 }} onClick={submit}>
          Calculate payoff
        </button>
        {error && <div className="notice" style={{ marginTop: 10 }}>{error}</div>}
        {result && (
          <div className="grid grid-3" style={{ marginTop: 12 }}>
            <div className="stat">
              <span className="stat-label">Net premium</span>
              <span className="stat-value small">{result.net_premium}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Max profit</span>
              <span className="stat-value small">{result.max_profit}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Max loss</span>
              <span className="stat-value small">{result.max_loss}</span>
            </div>
          </div>
        )}
      </div>
    </details>
  );
}

export function StrategyLabTab({ ticker }: { ticker: string }) {
  const lab = useAsync(() => api.getStrategyLab(ticker), [ticker]);

  if (lab.loading && !lab.data) return <LoadingState label="Loading strategy candidates…" />;
  if (lab.error && !lab.data) return <ErrorState message={lab.error} />;
  if (!lab.data) return null;

  return (
    <div>
      <StrategyLabStateBar lab={lab.data} />

      {lab.data.strategies.length === 0 ? (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            {lab.data.reason ??
              `No real options-chain data is available yet for ${ticker}'s upcoming earnings, so no deterministic strategy candidates can be generated. This requires a real options-chain snapshot (see the Options chain step in preparation) and a known upcoming earnings date.`}
          </p>
        </div>
      ) : (
        <>
          {lab.data.anchor === "general_current" && lab.data.reason && (
            <div className="notice">{lab.data.reason}</div>
          )}
          <p className="text-sm text-muted" style={{ marginTop: 0 }}>
            {lab.data.strategies.length} deterministic candidates, ranked by real payoff at the
            market's own implied move (±{pct(lab.data.implied_move_pct)}) relative to each
            strategy's own maximum loss — expiration {lab.data.expiration}, underlying{" "}
            {lab.data.underlying_price ? formatMoney(lab.data.underlying_price) : "—"}. Every
            number is computed from real, already-ingested market data — never a probability or
            profit guarantee.
          </p>
          <div className="grid grid-2">
            {lab.data.strategies.map((s) => (
              <StrategyCard key={s.rank} strategy={s} ticker={ticker} />
            ))}
          </div>
        </>
      )}

      {lab.data.chain.length > 0 && <OptionChainTable chain={lab.data.chain} />}

      <ManualStrategyCalculator />
    </div>
  );
}
