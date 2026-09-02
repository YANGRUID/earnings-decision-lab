import { api } from "../../../api/client";
import { useAsync } from "../../../hooks/useAsync";
import { EmptyState, ErrorState, LoadingState } from "../../StatusStates";
import { ForwardOutcomePanel } from "../../../pages/V4DecisionLab";
import { OfficialDecisionCard } from "../DecisionTab";
import { CONFIG_ORDER } from "../../v4/shared";
import { SectionHeader } from "../../v4/ui";

// Forward Outcome (Sections 23-27): V4 lifecycle per configuration, then
// the V3 control's official observation for the same company -- separate
// panels, never merged.
export function ForwardOutcomeTab({ ticker }: { ticker: string }) {
  const v4 = useAsync(() => api.getV4ShadowDecisions({ ticker, limit: 1 }), [ticker]);
  const d = v4.data?.decisions[0] ?? null;
  const cfgs = useAsync(() => (d ? api.getV4ShadowConfigurations(d.id) : Promise.resolve(null)), [d?.id]);
  if (v4.loading && !v4.data) return <LoadingState label="Loading forward outcome…" />;
  if (v4.error && !v4.data) return <ErrorState message={v4.error} />;
  return (
    <div>
      <SectionHeader title="V4 forward outcome" eyebrow="Experimental shadow" />
      {!d ? (
        <EmptyState><strong>No V4 observation for {ticker} yet.</strong> Forward outcomes appear only after a natural 15:30 ET shadow decision.</EmptyState>
      ) : cfgs.loading && !cfgs.data ? <LoadingState /> : cfgs.data ? (
        CONFIG_ORDER.map((k) => {
          const cfg = cfgs.data!.configurations.find((c) => c.configuration_key === k) ?? null;
          return cfg ? <ForwardOutcomePanel key={k} cfg={cfg} entry={cfgs.data!.entry_observation} settlement={cfgs.data!.settlement} policy={cfgs.data!.settlement_policy} /> : null;
        })
      ) : null}
      <div style={{ marginTop: 18 }}>
        <SectionHeader title="V3 control observation" eyebrow="Historical control · 15:55 ET" />
        <OfficialDecisionCard ticker={ticker} />
      </div>
    </div>
  );
}
