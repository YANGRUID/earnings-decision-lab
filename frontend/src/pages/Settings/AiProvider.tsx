import { useState } from "react";
import { useAsync } from "../../hooks/useAsync";
import { api, ApiError } from "../../api/client";
import { LoadingState, ErrorState } from "../../components/StatusStates";
import { ProviderCredentialForm } from "../../components/settings/ProviderCredentialForm";
import { CREDENTIAL_PROVIDERS, providerLabel, formatRelativeTime } from "../../lib/format";
import type { ProviderStatus } from "../../types/api";

function StatusPill({ status }: { status: ProviderStatus }) {
  if (!status.configured) {
    return <span className="pill pill-neutral">not configured</span>;
  }
  if (status.last_error_status && status.last_error_at) {
    return <span className="pill pill-negative">{status.last_error_status}</span>;
  }
  return <span className="pill pill-positive">configured</span>;
}

function ProviderRow({
  status,
  isActive,
  onActivate,
  onTested,
}: {
  status: ProviderStatus;
  isActive: boolean;
  onActivate: () => void;
  onTested: () => void;
}) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const runTest = async () => {
    setTesting(true);
    setResult(null);
    try {
      const outcome = await api.testProviderConnection("llm", status.provider);
      setResult(
        outcome.status === "connected"
          ? "Connected"
          : `${outcome.status}${outcome.detail ? `: ${outcome.detail}` : ""}`
      );
      onTested();
    } catch (err) {
      setResult(err instanceof ApiError ? err.message : "Test failed.");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="card" style={{ marginBottom: 10, borderColor: isActive ? "var(--color-accent)" : undefined }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <strong>{providerLabel(status.provider)}</strong>{" "}
          {isActive && <span className="pill pill-positive">active</span>}{" "}
          <span className="mono text-muted text-sm">{status.masked_key ?? "no API key set"}</span>
        </div>
        <StatusPill status={status} />
      </div>
      <div className="grid grid-3" style={{ gap: 10, marginTop: 10 }}>
        <div className="stat">
          <span className="stat-label">Last successful test</span>
          <span className="stat-value small">{formatRelativeTime(status.last_success_at)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Set as active provider</span>
          <button className="btn-secondary" onClick={onActivate} disabled={isActive || !status.configured}>
            {isActive ? "Active" : "Use this provider"}
          </button>
        </div>
        <div className="stat">
          <span className="stat-label">Test connection</span>
          <button className="btn-secondary" onClick={runTest} disabled={testing || !status.configured}>
            {testing ? "Testing…" : "Test Connection"}
          </button>
        </div>
      </div>
      {CREDENTIAL_PROVIDERS.includes(status.provider as (typeof CREDENTIAL_PROVIDERS)[number]) && (
        <div style={{ marginTop: 10 }}>
          <ProviderCredentialForm
            provider={status.provider}
            configured={status.configured}
            needsBaseUrl={status.provider === "openai_compatible"}
            onChanged={onTested}
          />
        </div>
      )}
      {!status.configured && (
        <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
          No API key configured for this provider — add one above to switch to it.
        </p>
      )}
      {status.last_error_detail && (
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

export function AiProvider() {
  const dashboard = useAsync(() => api.getProviderDashboard(), []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [modelDraft, setModelDraft] = useState("");

  if (dashboard.loading && !dashboard.data) return <LoadingState label="Loading AI provider settings…" />;
  if (dashboard.error && !dashboard.data) return <ErrorState message={dashboard.error} />;
  if (!dashboard.data) return null;

  const domain = dashboard.data.domains.find((d) => d.domain === "llm");
  if (!domain) return null;

  const activate = async (provider: string) => {
    setSaving(true);
    setError(null);
    try {
      await api.updateProviderSettings({ llm_provider: provider });
      dashboard.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not switch AI provider.");
    } finally {
      setSaving(false);
    }
  };

  const applyModel = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.updateProviderSettings({ llm_model: modelDraft.trim() || null });
      dashboard.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not update model override.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <div className="page-header">
        <h1>AI Provider</h1>
        <p>
          Which LLM answers research questions and generates the AI Earnings Thesis. Only providers
          with a real adapter in this codebase are shown. Add or replace a provider's API key
          directly below — it's encrypted before it's stored, and this page never shows the real
          key back, only a masked suffix once it's saved.
        </p>
      </div>
      {error && <div className="notice">{error}</div>}
      <div className="card" style={{ marginBottom: 20, maxWidth: 480 }}>
        <div className="field">
          <label>Strategy risk preference</label>
          <p className="text-sm text-muted" style={{ marginTop: 0 }}>
            Caps which strategy categories the AI Options Decision Engine and Strategy Lab may
            surface. Uncovered/naked short options are never generated by this version of the
            product regardless of this setting — see docs/limitations.md.
          </p>
          <select
            value={dashboard.data.strategy_risk_preference}
            disabled={saving}
            onChange={async (e) => {
              setSaving(true);
              setError(null);
              try {
                await api.updateProviderSettings({ strategy_risk_preference: e.target.value });
                dashboard.reload();
              } catch (err) {
                setError(err instanceof ApiError ? err.message : "Could not update risk preference.");
              } finally {
                setSaving(false);
              }
            }}
          >
            <option value="defined_risk_only">Defined Risk Only (spreads, condors — default)</option>
            <option value="allow_single_leg_long">Allow Single-Leg Long Options (+ long call/put)</option>
          </select>
        </div>
      </div>
      <div className="card" style={{ marginBottom: 20, maxWidth: 480 }}>
        <div className="field">
          <label>Model override (optional)</label>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              type="text"
              placeholder="leave blank to use the provider's default model"
              value={modelDraft}
              onChange={(e) => setModelDraft(e.target.value)}
              disabled={saving}
            />
            <button className="btn-secondary" onClick={applyModel} disabled={saving}>
              Apply
            </button>
          </div>
        </div>
      </div>
      {domain.providers.map((p) => (
        <ProviderRow
          key={p.provider}
          status={p}
          isActive={p.provider === domain.primary}
          onActivate={() => activate(p.provider)}
          onTested={dashboard.reload}
        />
      ))}
    </div>
  );
}
