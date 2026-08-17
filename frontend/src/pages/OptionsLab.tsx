import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { OptionLegInput, StrategyPayoffResponse, ImpliedMoveResponse } from "../types/api";

const PRESETS: Record<string, OptionLegInput[]> = {
  "Long call": [{ option_type: "call", action: "buy", strike: "100", premium: "5" }],
  "Long put": [{ option_type: "put", action: "buy", strike: "100", premium: "4" }],
  "Bull call spread": [
    { option_type: "call", action: "buy", strike: "100", premium: "6" },
    { option_type: "call", action: "sell", strike: "110", premium: "2" },
  ],
  "Bear put spread": [
    { option_type: "put", action: "buy", strike: "110", premium: "8" },
    { option_type: "put", action: "sell", strike: "100", premium: "3" },
  ],
  "Long straddle": [
    { option_type: "call", action: "buy", strike: "100", premium: "6" },
    { option_type: "put", action: "buy", strike: "100", premium: "5" },
  ],
  "Iron condor": [
    { option_type: "put", action: "buy", strike: "90", premium: "1" },
    { option_type: "put", action: "sell", strike: "95", premium: "2" },
    { option_type: "call", action: "sell", strike: "105", premium: "2" },
    { option_type: "call", action: "buy", strike: "110", premium: "1" },
  ],
};

function emptyLeg(): OptionLegInput {
  return { option_type: "call", action: "buy", strike: "", premium: "" };
}

export function OptionsLab() {
  const [label, setLabel] = useState("Bull call spread");
  const [legs, setLegs] = useState<OptionLegInput[]>(PRESETS["Bull call spread"]);
  const [result, setResult] = useState<StrategyPayoffResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const applyPreset = (name: string) => {
    setLabel(name);
    setLegs(PRESETS[name].map((l) => ({ ...l })));
    setResult(null);
  };

  const updateLeg = (index: number, patch: Partial<OptionLegInput>) => {
    setLegs((prev) => prev.map((leg, i) => (i === index ? { ...leg, ...patch } : leg)));
  };

  const submit = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.calculatePayoff({ strategy_label: label, legs });
      setResult(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Calculation failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div className="page-header">
        <h1>Options Lab</h1>
        <p>
          Deterministic strategy payoff calculator — enter your own strikes and premiums; this
          never depends on live market data.
        </p>
      </div>

      <div className="card">
        <h2>Strategy</h2>
        <div className="field">
          <label>Preset</label>
          <select value={label} onChange={(e) => applyPreset(e.target.value)}>
            {Object.keys(PRESETS).map((name) => (
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
              <th></th>
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
                <td>
                  <button
                    className="btn-secondary"
                    style={{ padding: "4px 8px" }}
                    onClick={() => setLegs((prev) => prev.filter((_, idx) => idx !== i))}
                  >
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
          <button className="btn-secondary" onClick={() => setLegs((prev) => [...prev, emptyLeg()])}>
            + Add leg
          </button>
          <button className="btn" onClick={submit} disabled={loading}>
            {loading ? "Calculating…" : "Calculate payoff"}
          </button>
        </div>
      </div>

      {error && <div className="notice">{error}</div>}

      {result && (
        <div className="card">
          <h2>Result</h2>
          <p style={{ marginTop: 0 }}>{result.summary}</p>
          <div className="grid grid-3">
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
          <div style={{ marginTop: 10 }}>
            <span className="stat-label">Breakeven(s)</span>{" "}
            <span className="mono">{result.breakevens.join(", ") || "—"}</span>
          </div>
        </div>
      )}

      <ImpliedMoveCalculator />
    </div>
  );
}

function ImpliedMoveCalculator() {
  const [underlying, setUnderlying] = useState("114.50");
  const [strike, setStrike] = useState("115");
  const [callPrice, setCallPrice] = useState("4.30");
  const [putPrice, setPutPrice] = useState("4.10");
  const [result, setResult] = useState<ImpliedMoveResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    try {
      const res = await api.calculateImpliedMove({
        underlying_price: underlying,
        strike,
        call_price: callPrice,
        put_price: putPrice,
      });
      setResult(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Calculation failed.");
    }
  };

  return (
    <div className="card">
      <h2>Implied move (ATM straddle)</h2>
      <p className="text-sm text-muted">
        No live options-chain data is available — enter quotes directly (e.g. from a broker) to
        compute the implied move.
      </p>
      <div className="grid grid-2" style={{ maxWidth: 420 }}>
        <div className="field">
          <label>Underlying price</label>
          <input value={underlying} onChange={(e) => setUnderlying(e.target.value)} />
        </div>
        <div className="field">
          <label>ATM strike</label>
          <input value={strike} onChange={(e) => setStrike(e.target.value)} />
        </div>
        <div className="field">
          <label>Call price</label>
          <input value={callPrice} onChange={(e) => setCallPrice(e.target.value)} />
        </div>
        <div className="field">
          <label>Put price</label>
          <input value={putPrice} onChange={(e) => setPutPrice(e.target.value)} />
        </div>
      </div>
      <button className="btn" onClick={submit}>
        Calculate
      </button>
      {error && <div className="notice">{error}</div>}
      {result && (
        <div style={{ marginTop: 12 }}>
          <span className="stat-label">Implied move</span>{" "}
          <span className="stat-value small">
            {(Number(result.implied_move_pct) * 100).toFixed(2)}% (${result.implied_move_absolute})
          </span>
        </div>
      )}
    </div>
  );
}
