import { useState } from "react";
import { useAsync } from "../../hooks/useAsync";
import { invalidateStatus } from "../../lib/statusCache";
import { api, ApiError } from "../../api/client";
import { LoadingState, ErrorState } from "../../components/StatusStates";
import { formatRelativeTime } from "../../lib/format";
import type { IbkrStatus, TwsStatus } from "../../types/api";

function overallState(ibkr: IbkrStatus): { label: string; tone: "positive" | "negative" | "neutral" } {
  if (!ibkr.gateway_reachable) return { label: "Gateway offline", tone: "negative" };
  if (ibkr.competing) return { label: "Competing session (e.g. TWS) holds the connection", tone: "negative" };
  if (!ibkr.authenticated) return { label: "Gateway running, not authenticated", tone: "negative" };
  if (!ibkr.connected) return { label: "Authenticated, session not connected", tone: "negative" };
  return { label: "Gateway running and authenticated", tone: "positive" };
}

// Phase 4.8A -- the three-icon summary the in-app "Connect IBKR" workflow
// asks for. Collapses status_label's four precise values (see
// services/system_status.py::ibkr_status_label) down to three glanceable
// buckets: COMPETING_SESSION is folded into the same red/negative bucket
// as AUTH_REQUIRED (a real, distinct cause -- still shown verbatim in the
// "IBKR: ..." line and the detail stats below, never hidden) since both
// mean "not currently usable, needs your attention", while GATEWAY_
// UNREACHABLE gets its own neutral "offline" treatment (the container/
// process isn't even up, not a session problem).
function emojiStatus(ibkr: IbkrStatus): { emoji: string; label: string } {
  if (ibkr.status_label === "CONNECTED") return { emoji: "🟢", label: "Connected" };
  if (ibkr.status_label === "GATEWAY_UNREACHABLE") return { emoji: "⚪", label: "Gateway Offline" };
  if (ibkr.status_label === "COMPETING_SESSION") return { emoji: "🔴", label: "Competing Session" };
  return { emoji: "🔴", label: "Authentication Required" };
}

// IBKR TWS Migration, Phase 3 readiness (Section 20/21) -- the TWS-
// transport sibling of overallState/emojiStatus above (those two, the
// existing Web/Client-Portal-Gateway summaries, are unchanged). Keys
// primarily off reconnect_state where it's more specific than
// status_label's four coarse buckets (RECONNECTING has no status_label
// of its own -- see services/system_status.py::TwsStatus's own comment).
function twsState(tws: TwsStatus): { label: string; tone: "positive" | "negative" | "neutral" | "warning" } {
  if (tws.reconnect_state === "reconnecting") return { label: "Reconnecting", tone: "warning" };
  if (tws.status_label === "CONNECTED") return { label: "Ready", tone: "positive" };
  if (tws.status_label === "AUTH_REQUIRED") return { label: "Authentication required", tone: "negative" };
  if (tws.reconnect_state === "failed") return { label: "Failed", tone: "negative" };
  return { label: "Disconnected", tone: "negative" };
}

function twsEmojiStatus(tws: TwsStatus): { emoji: string; label: string } {
  const state = twsState(tws);
  if (state.tone === "positive") return { emoji: "🟢", label: state.label };
  if (state.tone === "warning") return { emoji: "🟡", label: state.label };
  if (tws.status_label === "GATEWAY_UNREACHABLE") return { emoji: "⚪", label: state.label };
  return { emoji: "🔴", label: state.label };
}

// --------------------------------------------------------------------------
// Section 21/22 -- IB Gateway / TWS API status card. Deliberately never
// shows a "Connect IBKR" browser-login button (Section 34: that flow only
// makes sense for the Web/Client-Portal Gateway below) or any Client
// Portal-specific copy/URL -- TWS authentication happens manually inside
// the IB Gateway desktop application itself, entirely outside this app,
// exactly like the Web card below never sees a password either.
// --------------------------------------------------------------------------

function TwsGatewayCard({ tws, onRefresh, refreshing }: { tws: TwsStatus; onRefresh: () => void; refreshing: boolean }) {
  const state = twsState(tws);
  const emoji = twsEmojiStatus(tws);

  return (
    <div className="card" style={{ marginBottom: 20, maxWidth: 640 }}>
      <strong>Status</strong>
      <p className="text-sm text-muted" style={{ marginTop: 4 }}>
        Provider: IB Gateway / TWS API
      </p>
      <div style={{ fontSize: "1.5rem", fontWeight: 600, marginTop: 6, marginBottom: 16 }}>
        {emoji.emoji} {emoji.label}
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <button className="btn-secondary" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh Status"}
        </button>
      </div>

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          marginTop: 22,
        }}
      >
        <strong>Connection detail</strong>
        <span className={`pill pill-${state.tone}`}>{state.label}</span>
      </div>
      <div className="grid grid-2" style={{ gap: 10, marginTop: 14 }}>
        <div className="stat">
          <span className="stat-label">Gateway reachable</span>
          <span className="stat-value small">{tws.gateway_reachable ? "Yes" : "No"}</span>
        </div>
        <div className="stat">
          <span className="stat-label">API socket connected</span>
          <span className="stat-value small">{tws.socket_connected ? "Yes" : "No"}</span>
        </div>
        <div className="stat">
          <span className="stat-label">API ready</span>
          <span className="stat-value small">{tws.api_ready ? "Yes" : "No"}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Market data</span>
          {/* IBKR TWS Migration, post-cutover cleanup (A10) -- market-data
              quality is only ever learned from a real marketDataType
              callback, so it is genuinely unknown until the first quote
              after a cold restart. Say that plainly instead of an
              unexplained dash; never fabricate "delayed" before IBKR
              has actually reported it. */}
          <span className="stat-value small">
            {tws.market_data_quality ?? "Unknown — awaiting first market-data observation"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Last heartbeat</span>
          <span className="stat-value small">{formatRelativeTime(tws.last_heartbeat)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Connection state</span>
          <span className="stat-value small mono">{tws.reconnect_state}</span>
        </div>
      </div>
      {tws.error && (
        <div className="notice" style={{ marginTop: 14, marginBottom: 0 }}>
          {tws.error}
        </div>
      )}
      {tws.status_label !== "CONNECTED" && (
        <div className="notice" style={{ marginTop: 14, marginBottom: 0 }}>
          <strong>IB Gateway login required.</strong> Authentication is performed directly in IB
          Gateway, not on this page:
          <ol style={{ marginTop: 6, marginBottom: 0, paddingLeft: 20 }}>
            <li>Open IB Gateway on this Mac</li>
            <li>Complete login / 2FA there</li>
            <li>Return here</li>
            <li>Click "Refresh Status" above</li>
          </ol>
        </div>
      )}
    </div>
  );
}

export function Ibkr() {
  const status = useAsync(() => api.getSystemStatus(), []);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  if (status.loading && !status.data) return <LoadingState label="Checking Interactive Brokers connectivity…" />;
  if (status.error && !status.data) return <ErrorState message={status.error} />;
  if (!status.data) return null;

  const { ibkr, tws } = status.data;
  // IBKR TWS Migration, Phase 3 readiness (Section 20) -- the single,
  // authoritative signal for which transport is actually configured:
  // tws.configured mirrors settings.ibkr_provider == "tws" exactly (see
  // services/system_status.py::get_tws_status), so this page never
  // re-derives that from anything client-side.
  const usingTws = tws.configured;
  const state = overallState(ibkr);
  const emoji = emojiStatus(ibkr);
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
      invalidateStatus("system-status");
    status.reload();
    } catch (err) {
      setResult(err instanceof ApiError ? err.message : "Test failed.");
    } finally {
      setTesting(false);
    }
  };

  // Phase 4.8A -- "Connect IBKR": the backend hands back nothing but a
  // URL (GET /ibkr/connect never sees or asks for a password); the real
  // login -- username/password, IBKR Mobile 2FA approval -- happens on
  // IBKR's own Gateway page, in this new tab, entirely outside this app.
  const connectIbkr = async () => {
    setConnecting(true);
    setConnectError(null);
    // Opened synchronously, inside this click handler's own call stack --
    // browsers can silently block window.open() once a click's "user
    // gesture" has expired, which the await below (a real network round
    // trip) risks doing. Deliberately no noopener/noreferrer: this tab's
    // destination is a fixed, trusted URL this app controls, never user
    // input, so the reverse-tabnabbing risk those flags guard against
    // doesn't apply -- and dropping them is what keeps `popup` a live
    // handle this can still navigate once the real URL is known, instead
    // of window.open() returning null.
    const popup = window.open("", "_blank");
    try {
      const { url } = await api.getIbkrConnectUrl();
      if (popup) {
        popup.location.href = url;
      } else {
        setConnectError("Your browser blocked the new tab — allow pop-ups for this site and try again.");
      }
    } catch (err) {
      popup?.close();
      setConnectError(err instanceof ApiError ? err.message : "Could not reach the backend.");
    } finally {
      setConnecting(false);
    }
  };

  const refreshStatus = () => {
    setConnectError(null);
    invalidateStatus("system-status");
    status.reload();
  };

  return (
    <div>
      <div className="page-header">
        <h1>Interactive Brokers</h1>
        {usingTws ? (
          <p>
            This page reflects the real, live state of this deployment's IB Gateway / TWS API
            connection — a single, persistent socket connection this backend process owns.
            Authentication happens manually inside the IB Gateway desktop application itself, on
            this Mac; this backend application never holds your IBKR username, password, or 2FA
            code. The Web/Client-Portal-Gateway path below this section still exists as manual
            rollback support, but is not the transport currently configured.
          </p>
        ) : (
          <p>
            This page reflects the real, live session state of whichever Gateway is configured —
            either one you run yourself and log into by hand at{" "}
            <span className="mono">https://localhost:5001</span>, or (Phase 4.8A, optional) the
            automated <span className="mono">ibkr-gateway</span> container that logs in on your
            behalf so the session stays up without a human re-authenticating every few hours. Either
            way, this backend application never holds your IBKR username or password itself — only
            the automated container's own environment does, when that path is configured. See
            docs/ibkr_gateway_runtime.md for the full setup and security model.
          </p>
        )}
      </div>

      {usingTws && <TwsGatewayCard tws={tws} onRefresh={refreshStatus} refreshing={status.loading} />}

      {!usingTws && (
      <div className="card" style={{ marginBottom: 20, maxWidth: 640 }}>
        <strong>Status</strong>
        <div style={{ fontSize: "1.5rem", fontWeight: 600, marginTop: 6, marginBottom: 16 }}>
          {emoji.emoji} {emoji.label}
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <button className="btn" onClick={connectIbkr} disabled={connecting}>
            {connecting ? "Opening…" : "Connect IBKR"}
          </button>
          <button className="btn-secondary" onClick={refreshStatus} disabled={status.loading}>
            {status.loading ? "Refreshing…" : "Refresh Status"}
          </button>
        </div>
        {connectError && (
          <div className="notice" style={{ marginTop: 14, marginBottom: 0 }}>
            {connectError}
          </div>
        )}
        <p className="text-sm text-muted" style={{ marginTop: 14, marginBottom: 0 }}>
          "Connect IBKR" opens the Gateway's own login page in a new tab — enter your IBKR
          username/password and approve the IBKR Mobile 2FA prompt there. This application never
          sees any of that; it only opens the page and, afterward, reads the Gateway's real session
          status back via "Refresh Status".
        </p>

        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            marginTop: 22,
          }}
        >
          <strong>Gateway session detail</strong>
          <span className={`pill pill-${state.tone}`}>{state.label}</span>
        </div>
        <p className="mono text-sm" style={{ marginTop: 6, marginBottom: 0 }}>
          IBKR: {ibkr.status_label}
        </p>
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
            How to reconnect: no Gateway process is reachable at all yet — this is a step before
            "Connect IBKR" can help. If you're using the automated container, run{" "}
            <span className="mono">docker compose up -d ibkr-gateway</span> (see
            docs/ibkr_gateway_runtime.md); if you're running the Gateway manually, start it on this
            machine. Then use "Refresh Status" above.
          </p>
        )}
        {ibkr.gateway_reachable && !ibkr.authenticated && !ibkr.competing && (
          <p className="text-sm text-muted" style={{ marginTop: 14, marginBottom: 0 }}>
            How to reconnect: the Gateway is running but your session has expired or was never
            logged in — click "Connect IBKR" above, log in, then "Refresh Status".
          </p>
        )}
      </div>
      )}

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
                {ibkrProvider.last_error_status &&
                ibkrProvider.last_error_at &&
                (!ibkrProvider.last_success_at ||
                  new Date(ibkrProvider.last_error_at).getTime() > new Date(ibkrProvider.last_success_at).getTime())
                  ? `${ibkrProvider.last_error_status} (${formatRelativeTime(ibkrProvider.last_error_at)})`
                  : "none since the last successful snapshot"}
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
        No username, password, or 2FA code is ever entered on this page, and this backend
        application never reads or stores them — read-only, always: no order-placement,
        modification, or cancellation endpoint is ever called against either Gateway. See
        docs/ibkr_integration.md (manual login) and docs/ibkr_gateway_runtime.md (Phase 4.8A
        automated login) for the full architecture.
      </p>
    </div>
  );
}
