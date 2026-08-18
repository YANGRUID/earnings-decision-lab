import { useAsync } from "../../hooks/useAsync";
import { api } from "../../api/client";
import { ErrorState, LoadingState } from "../StatusStates";
import { formatMoney } from "../../lib/format";

export function ExposureTab({ ticker }: { ticker: string }) {
  const positions = useAsync(() => api.getPortfolioPositions({ ticker }), [ticker]);

  if (positions.loading) return <LoadingState label="Loading real IBKR exposure…" />;
  if (positions.error) return <ErrorState message={positions.error} />;
  if (!positions.data) return null;

  return (
    <div>
      <p className="text-sm text-muted" style={{ marginTop: 0 }}>
        Real, read-only positions from your Interactive Brokers account matching {ticker} — never
        a market quote, and this tool never places, modifies, or cancels an order.
      </p>

      {positions.data.positions.length === 0 ? (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            {positions.data.snapshot_timestamp
              ? `No real ${ticker} position on record as of the last IBKR snapshot.`
              : "No real IBKR portfolio snapshot has been collected yet."}
          </p>
        </div>
      ) : (
        <div className="card">
          <h2>Positions</h2>
          <table>
            <thead>
              <tr>
                <th>Contract</th>
                <th>Asset class</th>
                <th>Quantity</th>
                <th>Market value</th>
                <th>Unrealized P&amp;L</th>
                <th>Account</th>
              </tr>
            </thead>
            <tbody>
              {positions.data.positions.map((p) => (
                <tr key={p.conid}>
                  <td className="mono">{p.contract_description}</td>
                  <td className="mono">{p.asset_class}</td>
                  <td className="mono">{p.quantity}</td>
                  <td className="mono">{p.market_value ? `$${formatMoney(p.market_value)}` : "—"}</td>
                  <td className={`mono ${Number(p.unrealized_pnl ?? 0) >= 0 ? "positive" : "negative"}`}>
                    {p.unrealized_pnl ? `$${formatMoney(p.unrealized_pnl)}` : "—"}
                  </td>
                  <td className="mono">{p.account_id_masked}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-sm text-faint" style={{ marginTop: 10, marginBottom: 0 }}>
            As of {new Date(positions.data.snapshot_timestamp!).toLocaleString()}.
          </p>
        </div>
      )}
    </div>
  );
}
