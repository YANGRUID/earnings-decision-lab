import { api } from "../../../api/client";
import { useAsync } from "../../../hooks/useAsync";
import { EmptyState, ErrorState, LoadingState } from "../../StatusStates";
import { Metric, Notice, ProvenanceBadge, SectionHeader, Timestamp } from "../../v4/ui";

// Market View (Section 5): the DecisionView as an AI judgment -- clearly
// separated from the deterministic ranking that consumes it.
export function MarketViewTab({ ticker }: { ticker: string }) {
  const v4 = useAsync(() => api.getV4ShadowDecisions({ ticker, limit: 1 }), [ticker]);
  if (v4.loading && !v4.data) return <LoadingState label="Loading market view…" />;
  if (v4.error && !v4.data) return <ErrorState message={v4.error} />;
  const d = v4.data?.decisions[0] ?? null;
  if (!d || !d.view) {
    return (
      <EmptyState>
        <strong>No V4 market view for {ticker} yet.</strong> A DecisionView is generated once, at the 15:30 ET
        decision window, from prepared research. Nothing is generated on demand for the forward test.
      </EmptyState>
    );
  }
  const v = d.view;
  return (
    <div>
      <Notice kind="info" testId="ai-judgment-notice">
        <strong>AI judgment, not a ranking.</strong> This view is the model's assessment of direction and
        move. The strategy recommendation is produced separately by the deterministic engine.
      </Notice>
      <div className="card" data-testid="market-view">
        <SectionHeader title="Market view" eyebrow={ticker} right={<ProvenanceBadge provider={d.provenance?.llm_provider} model={d.provenance?.llm_model} version={d.provenance?.prompt_version} />} />
        <div className="grid grid-4" style={{ gap: 10 }}>
          <Metric label="Direction" value={(v.direction ?? "—").toUpperCase()} />
          <Metric label="Move / volatility view" value={(v.expected_move_intent ?? "—").replace(/_/g, " ").toUpperCase()} sub={v.volatility ?? undefined} />
          <Metric label="Confidence" value={(v.confidence ?? "—").toUpperCase()} sub={<strong>NOT A PROBABILITY</strong>} />
          <Metric label="As of" value={<Timestamp iso={d.as_of} />} />
        </div>
        {v.reasoning && (
          <>
            <h3 style={{ marginTop: 14 }}>Reasoning</h3>
            <p className="text-muted" style={{ margin: 0, whiteSpace: "pre-wrap" }}>{v.reasoning}</p>
          </>
        )}
        <details style={{ marginTop: 10 }}>
          <summary className="text-muted text-sm">Evidence provenance (advanced)</summary>
          <table style={{ marginTop: 6, fontSize: ".8rem" }}>
            <tbody>
              <tr><td className="text-muted">Prompt version</td><td className="mono">{d.provenance?.prompt_version ?? "—"}</td></tr>
              <tr><td className="text-muted">View schema</td><td className="mono">{d.provenance?.decision_view_schema_version ?? "—"}</td></tr>
              <tr><td className="text-muted">Timing policy</td><td className="mono">{d.timing_policy_version ?? "—"}</td></tr>
            </tbody>
          </table>
        </details>
      </div>
    </div>
  );
}
