import { UpcomingEarningsTab } from "../UpcomingEarningsTab";
import { Metric, SectionHeader } from "../../v4/ui";
import type { ResearchOverview } from "../../../types/api";

// Earnings Setup: timing policy, market expectations and options pricing
// for the next unreported event.
export function EarningsSetupTab({ ticker, overview, onOverviewChanged }: { ticker: string; overview: ResearchOverview; onOverviewChanged: () => void }) {
  return (
    <div>
      <div className="card" data-testid="timing-policy">
        <SectionHeader title="V4 timing policy" />
        <div className="grid grid-4" style={{ gap: 10 }}>
          <Metric label="Decision / entry" value="15:30 ET" mono sub="legal pre-earnings trading day" />
          <Metric label="Settlement" value="15:30 ET" mono sub="first post-earnings trading day (T+1)" />
          <Metric label="Rule" value="AMC: D0 → D+1 · BMO: D−1 → D0" sub="never a same-day settlement" />
          <Metric label="Policy version" value="v4-1530-entry-1530-t1-settlement-v2" mono sub="frozen on every observation" />
        </div>
      </div>
      <UpcomingEarningsTab ticker={ticker} overview={overview} onOverviewChanged={onOverviewChanged} />
    </div>
  );
}
