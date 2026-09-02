import { useState } from "react";
import { useAsync } from "../../hooks/useAsync";
import { api, ApiError } from "../../api/client";
import { LoadingState, ErrorState } from "../../components/StatusStates";
import { ProviderCredentialForm } from "../../components/settings/ProviderCredentialForm";
import { CREDENTIAL_PROVIDERS, DOMAIN_LABELS, providerLabel, formatRelativeTime } from "../../lib/format";
import type { DomainStatus, ProviderStatus } from "../../types/api";

const DATA_DOMAINS = [
  "price_history",
  "earnings_estimates",
  "earnings_calendar",
  "filings",
  "options",
] as const;

/** An error is the provider's current state only while nothing has
 * succeeded after it -- the backend already drops superseded errors, and
 * this keeps the pill honest even against a stale response. */
function errorIsCurrent(status: ProviderStatus): boolean {
  if (!status.last_error_status || !status.last_error_at) return false;
  if (!status.last_success_at) return true;
  return new Date(status.last_error_at).getTime() > new Date(status.last_success_at).getTime();
}

function StatusPill({ status }: { status: ProviderStatus }) {
  if (!status.configured) {
    return <span className="pill pill-neutral">not configured</span>;
  }
  if (errorIsCurrent(status)) {
    return <span className="pill pill-negative">{status.last_error_status}</span>;
  }
  return <span className="pill pill-positive">configured</span>;
}

function ProviderRow({
  status,
  onTested,
  ibkrTransport,
}: {
  status: ProviderStatus;
  onTested: () => void;
  ibkrTransport?: "web" | "tws";
}) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const runTest = async () => {
    setTesting(true);
    setResult(null);
    try {
      const outcome = await api.testProviderConnection(status.domain, status.provider);
      setResult(outcome.status === "connected" ? "Connected" : `${outcome.status}${outcome.detail ? `: ${outcome.detail}` : ""}`);
      onTested();
    } catch (err) {
      setResult(err instanceof ApiError ? err.message : "Test failed.");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="card" style={{ marginBottom: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <strong>{providerLabel(status.provider)}</strong>{" "}
          <span className="mono text-muted text-sm">
            {status.masked_key ??
              (status.provider === "sec_edgar" || status.provider === "ibkr"
                ? "no key required"
                : "no API key configured")}
          </span>
        </div>
        <StatusPill status={status} />
      </div>
      {ibkrTransport && (
        <p className="text-sm text-muted" style={{ marginTop: 4, marginBottom: 0 }}>
          Active transport: <strong>{ibkrTransport === "tws" ? "TWS API (IB Gateway)" : "Web gateway (rollback path)"}</strong>
        </p>
      )}
      <div className="grid grid-3" style={{ gap: 10, marginTop: 10 }}>
        <div className="stat">
          <span className="stat-label">Last successful use</span>
          <span className="stat-value small">{formatRelativeTime(status.last_success_at)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Capabilities</span>
          <span className="stat-value small mono">
            {[
              status.capabilities.prices && "prices",
              status.capabilities.earnings_estimates && "estimates",
              status.capabilities.earnings_calendar && "calendar",
              status.capabilities.filings && "filings",
              status.capabilities.options && "options",
              status.capabilities.greeks && "greeks",
              status.capabilities.ai && "ai",
            ]
              .filter(Boolean)
              .join(", ") || "—"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Test connection</span>
          <button className="btn-secondary" onClick={runTest} disabled={testing}>
            {testing ? "Testing…" : "Test Connection"}
          </button>
        </div>
      </div>
      {CREDENTIAL_PROVIDERS.includes(status.provider as (typeof CREDENTIAL_PROVIDERS)[number]) && (
        <div style={{ marginTop: 10 }}>
          <ProviderCredentialForm
            provider={status.provider}
            configured={status.configured}
            onChanged={onTested}
          />
        </div>
      )}
      {status.entitlement_note && (
        <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
          {status.entitlement_note}
        </p>
      )}
      {status.last_error_detail && errorIsCurrent(status) && (
        <div className="notice" style={{ marginTop: 10, marginBottom: 0 }}>
          Last error ({formatRelativeTime(status.last_error_at)}): {status.last_error_detail}
        </div>
      )}
      {result && (
        <div className="notice" style={{ marginTop: 10, marginBottom: 0 }}>
          {result}
        </div>
      )}
    </div>
  );
}

function DomainCard({
  domain,
  onChanged,
  usingTws,
}: {
  domain: DomainStatus;
  onChanged: () => void;
  usingTws: boolean;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const supportsFallback = domain.domain === "price_history" || domain.domain === "options";

  const setPrimary = async (provider: string) => {
    setSaving(true);
    setError(null);
    try {
      const field = domain.domain === "price_history" ? "price_history_primary" : "options_primary";
      await api.updateProviderSettings({ [field]: provider });
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not update primary provider.");
    } finally {
      setSaving(false);
    }
  };

  const setFallback = async (provider: string) => {
    setSaving(true);
    setError(null);
    try {
      const field =
        domain.domain === "price_history" ? "price_history_fallback" : "options_fallback";
      if (provider === "") {
        const clearField =
          domain.domain === "price_history"
            ? "clear_price_history_fallback"
            : "clear_options_fallback";
        await api.updateProviderSettings({ [clearField]: true });
      } else {
        await api.updateProviderSettings({ [field]: provider });
      }
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not update fallback provider.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ marginBottom: 28 }}>
      <h2 style={{ marginBottom: 4 }}>{DOMAIN_LABELS[domain.domain] ?? domain.domain}</h2>
      {domain.domain === "earnings_calendar" ? (
        <p className="text-sm text-muted" style={{ marginTop: 0 }}>
          <strong>{providerLabel(domain.primary)}</strong> (primary),{" "}
          <strong>{providerLabel(domain.fallback)}</strong> (fallback) — not currently
          configurable from Settings.
        </p>
      ) : supportsFallback ? (
        <div className="grid grid-2" style={{ gap: 10, marginBottom: 14, maxWidth: 480 }}>
          <div className="field">
            <label>
              Primary provider {domain.primary_is_override && <span className="text-faint">(owner override)</span>}
            </label>
            <select value={domain.primary ?? ""} disabled={saving} onChange={(e) => setPrimary(e.target.value)}>
              {domain.providers.map((p) => (
                <option key={p.provider} value={p.provider}>
                  {providerLabel(p.provider)}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>
              Fallback provider{" "}
              {domain.fallback_is_override && <span className="text-faint">(owner override)</span>}
            </label>
            <select
              value={domain.fallback ?? ""}
              disabled={saving}
              onChange={(e) => setFallback(e.target.value)}
            >
              <option value="">None</option>
              {domain.providers
                .filter((p) => p.provider !== domain.primary)
                .map((p) => (
                  <option key={p.provider} value={p.provider}>
                    {providerLabel(p.provider)}
                  </option>
                ))}
            </select>
          </div>
        </div>
      ) : (
        <p className="text-sm text-muted" style={{ marginTop: 0 }}>
          Only one real provider exists for this domain today —{" "}
          <strong>{providerLabel(domain.primary)}</strong>. No selection needed.
        </p>
      )}
      {error && <div className="notice">{error}</div>}
      {domain.providers.map((p) => (
        <ProviderRow
          key={p.provider}
          status={p}
          onTested={onChanged}
          ibkrTransport={p.provider === "ibkr" ? (usingTws ? "tws" : "web") : undefined}
        />
      ))}
    </div>
  );
}

function CapabilityMatrix({ domains }: { domains: DomainStatus[] }) {
  const allProviders = domains.flatMap((d) => d.providers);
  const cols: (keyof ProviderStatus["capabilities"])[] = [
    "prices",
    "earnings_estimates",
    "earnings_calendar",
    "filings",
    "options",
    "greeks",
    "ai",
  ];
  return (
    <div className="card">
      <h2>Provider capability matrix</h2>
      <p className="text-sm text-muted" style={{ marginTop: 0 }}>
        Derived from the real adapters in this codebase, hand-reviewed — never claims a capability
        an adapter doesn't actually implement.
      </p>
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Provider</th>
              {cols.map((c) => (
                <th key={c}>{c.replace("_", " ")}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {allProviders.map((p) => (
              <tr key={`${p.domain}-${p.provider}`}>
                <td>{providerLabel(p.provider)}</td>
                {cols.map((c) => (
                  <td key={c} className="mono">
                    {p.capabilities[c] ? "✓" : "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function DataProviders() {
  const dashboard = useAsync(() => api.getProviderDashboard(), []);
  // IBKR TWS Migration, Phase 3 readiness (Section 31) -- fetched only to
  // read tws.configured, so this row can honestly say which transport is
  // active; a real, already-existing GET this app calls elsewhere too,
  // never a new endpoint.
  const status = useAsync(() => api.getSystemStatus(), []);

  if (dashboard.loading && !dashboard.data) return <LoadingState label="Loading provider settings…" />;
  if (dashboard.error && !dashboard.data) return <ErrorState message={dashboard.error} />;
  if (!dashboard.data) return null;

  const usingTws = status.data?.tws.configured ?? false;
  const domains = dashboard.data.domains.filter((d) =>
    (DATA_DOMAINS as readonly string[]).includes(d.domain)
  );

  return (
    <div>
      <div className="page-header">
        <h1>Data Providers</h1>
        <p>
          Which real provider serves each kind of data, whether it's actually connected right now,
          and how it falls back — never a silent substitution. Provider selection and credentials
          are owner settings: changing either changes which paid APIs get called (see
          docs/ibkr_integration.md and docs/data_sources.md). A key entered below is encrypted
          before storage and is never shown back — only a masked suffix.
        </p>
      </div>
      {domains.map((d) => (
        <DomainCard key={d.domain} domain={d} onChanged={dashboard.reload} usingTws={usingTws} />
      ))}
      <CapabilityMatrix domains={dashboard.data.domains} />
    </div>
  );
}
