import { useState } from "react";
import { useAsync } from "../../hooks/useAsync";
import { api, ApiError } from "../../api/client";
import { LoadingState, ErrorState } from "../../components/StatusStates";
import { providerLabel, formatRelativeTime } from "../../lib/format";
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
      {!status.configured && (
        <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
          No API key set in the backend's environment for this provider — add one to switch to it.
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
          with a real adapter in this codebase are shown; switching providers only works once that
          provider's API key is set in the backend's environment — this page never edits or displays
          the key itself, only which configured provider is active.
        </p>
      </div>
      {error && <div className="notice">{error}</div>}
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
