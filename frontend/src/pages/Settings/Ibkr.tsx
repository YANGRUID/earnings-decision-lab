import { useState } from "react";
import { useAsync } from "../../hooks/useAsync";
import { api, ApiError } from "../../api/client";
import { LoadingState, ErrorState } from "../../components/StatusStates";
import { formatRelativeTime } from "../../lib/format";
import type { IbkrStatus } from "../../types/api";

function overallState(ibkr: IbkrStatus): { label: string; tone: "positive" | "negative" | "neutral" } {
  if (!ibkr.gateway_reachable) return { label: "Gateway offline", tone: "negative" };
  if (ibkr.competing) return { label: "Competing session (e.g. TWS) holds the connection", tone: "negative" };
  if (!ibkr.authenticated) return { label: "Gateway running, not authenticated", tone: "negative" };
  if (!ibkr.connected) return { label: "Authenticated, session not connected", tone: "negative" };
  return { label: "Gateway running and authenticated", tone: "positive" };
}

export function Ibkr() {
  const status = useAsync(() => api.getSystemStatus(), []);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  if (status.loading && !status.data) return <LoadingState label="Checking IBKR Gateway status…" />;
  if (status.error && !status.data) return <ErrorState message={status.error} />;
  if (!status.data) return null;

  const { ibkr } = status.data;
  const state = overallState(ibkr);
  const optionsDomain = status.data.providers.domains.find((d) => d.domain === "options");
  const ibkrProvider = optionsDomain?.providers.find((p) => p.provider === "ibkr");

  const runTest = async () => {
    setTesting(true);
    setResult(null);
    try {
      const outcome = await api.testProviderConnection("options", "ibkr");
      setResult(
        outcome.status === "connected"
          ? "Connected"
          : `${outcome.status}${outcome.detail ? `: ${outcome.detail}` : ""}`
      );
      status.reload();
    } catch (err) {
      setResult(err instanceof ApiError ? err.message : "Test failed.");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div>
      <div className="page-header">
        <h1>Interactive Brokers</h1>
        <p>
          This project never holds IBKR credentials — the Client Portal Gateway runs on your own
          machine and you authenticate to it yourself at{" "}
          <span className="mono">https://localhost:5001</span>. This page only reflects the real,
          current session state reported by that Gateway.
        </p>
      </div>
      <div className="card" style={{ marginBottom: 20, maxWidth: 640 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <strong>Gateway session</strong>
          <span className={`pill pill-${state.tone}`}>{state.label}</span>
        </div>
        <div className="grid grid-2" style={{ gap: 10, marginTop: 14 }}>
          <div className="stat">
            <span className="stat-label">Gateway reachable</span>
            <span className="stat-value small">{ibkr.gateway_reachable ? "Yes" : "No"}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Authenticated</span>
            <span className="stat-value small">{ibkr.authenticated ? "Yes" : "No"}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Connected</span>
            <span className="stat-value small">{ibkr.connected ? "Yes" : "No"}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Competing session</span>
            <span className="stat-value small">{ibkr.competing ? "Yes" : "No"}</span>
          </div>
        </div>
        {ibkr.error && (
          <div className="notice" style={{ marginTop: 14, marginBottom: 0 }}>
            {ibkr.error}
          </div>
        )}
        {!ibkr.gateway_reachable && (
          <p className="text-sm text-muted" style={{ marginTop: 14, marginBottom: 0 }}>
            How to reconnect: start the IBKR Client Portal Gateway on this machine, open{" "}
            <span className="mono">https://localhost:5001</span> in a browser and log in there, then
            retry below.
          </p>
        )}
        {ibkr.gateway_reachable && !ibkr.authenticated && !ibkr.competing && (
          <p className="text-sm text-muted" style={{ marginTop: 14, marginBottom: 0 }}>
            How to reconnect: the Gateway is running but your session has expired or was never
            logged in — open <span className="mono">https://localhost:5001</span> and log in again.
          </p>
        )}
      </div>

      {ibkrProvider && (
        <div className="card" style={{ maxWidth: 640 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <strong>Options data via IBKR</strong>
            <button className="btn-secondary" onClick={runTest} disabled={testing}>
              {testing ? "Testing…" : "Test Connection"}
            </button>
          </div>
          <div className="grid grid-2" style={{ gap: 10, marginTop: 14 }}>
            <div className="stat">
              <span className="stat-label">Last successful snapshot</span>
              <span className="stat-value small">{formatRelativeTime(ibkrProvider.last_success_at)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Last error</span>
              <span className="stat-value small">
                {ibkrProvider.last_error_status
                  ? `${ibkrProvider.last_error_status} (${formatRelativeTime(ibkrProvider.last_error_at)})`
                  : "none recorded"}
              </span>
            </div>
          </div>
          {result && (
            <div className="notice" style={{ marginTop: 14, marginBottom: 0 }}>
              {result}
            </div>
          )}
        </div>
      )}
      <p className="text-sm text-faint" style={{ maxWidth: 640 }}>
        This project never automates IBKR login — no username, password, or 2FA code is ever
        entered here. See docs/ibkr_integration.md for the full architecture.
      </p>
    </div>
  );
}
