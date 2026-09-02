import { api } from "../../../api/client";
import { useAsync } from "../../../hooks/useAsync";
import { EmptyState, ErrorState, LoadingState } from "../../StatusStates";
import { V4DecisionView } from "../../../pages/V4DecisionLab";

// V4 Decision (Section 6) and Candidates -- the same embeddable view the
// V4 Decision Lab page uses, scoped to this company's latest decision.
export function V4DecisionTab({ ticker, mode }: { ticker: string; mode: "lab" | "explorer" }) {
  const v4 = useAsync(() => api.getV4ShadowDecisions({ ticker, limit: 1 }), [ticker]);
  if (v4.loading && !v4.data) return <LoadingState label="Loading V4 decision…" />;
  if (v4.error && !v4.data) return <ErrorState message={v4.error} />;
  const d = v4.data?.decisions[0] ?? null;
  if (!d) {
    return (
      <EmptyState>
        <strong>No V4 decision for {ticker} yet.</strong> The V4 shadow engine freezes one decision per
        earnings event at 15:30 ET on the legal pre-earnings trading day. Nothing here is simulated or
        generated on demand.
      </EmptyState>
    );
  }
  return <V4DecisionView decisionId={d.id} mode={mode} />;
}
