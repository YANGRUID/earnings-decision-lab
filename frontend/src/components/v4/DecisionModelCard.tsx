import { api } from "../../api/client";
import { useAsync } from "../../hooks/useAsync";

// The explicit V4 DecisionView model configuration (2026-09-02), read-only:
// it is set in the environment (V4_DECISION_VIEW_*), never from this page,
// and shown here so the active production state is unambiguous before the
// first natural forward sample. The API key is never part of the payload.
export function DecisionModelCard() {
  const dash = useAsync(() => api.getProviderDashboard(), []);
  const m = dash.data?.v4_decision_view ?? null;
  return (
    <div className="card" data-testid="v4-decision-model-card">
      <h2>V4 DecisionView model</h2>
      {!m ? (
        <p className="text-sm text-muted" style={{ margin: 0 }}>{dash.loading ? "Loading…" : "Not reported by this backend."}</p>
      ) : (
        <>
          <div className="grid grid-4" style={{ gap: 10 }}>
            <div className="stat"><span className="stat-label">Provider</span><span className="stat-value small">{m.provider ?? "—"}</span></div>
            <div className="stat"><span className="stat-label">Model</span><span className="stat-value small mono" data-testid="v4-decision-model">{m.model ?? "NOT CONFIGURED"}</span></div>
            <div className="stat"><span className="stat-label">Thinking</span><span className="stat-value small mono">{m.thinking ?? "—"}</span></div>
            <div className="stat"><span className="stat-label">Reasoning effort</span><span className="stat-value small mono">{m.thinking === "enabled" ? (m.reasoning_effort ?? "—") : "n/a"}</span></div>
          </div>
          <p className="text-sm text-muted" style={{ margin: "10px 0 0" }}>
            Token budget {m.max_tokens ?? "—"} (hidden reasoning counts against it) · config {m.config_version ?? "—"} · set via V4_DECISION_VIEW_* in the environment, separate from the general model above. Every frozen V4 view records this configuration and the model identity the API returned.
          </p>
          {m.config_error && (
            <div className="notice notice-critical" style={{ marginTop: 8 }} data-testid="v4-decision-model-error">
              Configuration error: {m.config_error}. V4 DecisionViews will fail rather than fall back to another model.
            </div>
          )}
        </>
      )}
    </div>
  );
}
