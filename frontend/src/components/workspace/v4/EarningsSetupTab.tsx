import { UpcomingEarningsTab } from "../UpcomingEarningsTab";
import { Metric, SectionHeader } from "../../v4/ui";
import type { ResearchOverview } from "../../../types/api";

// Earnings Setup (Section 4): timing, source, expected/historical move,
// research freshness, options readiness and the decision timing policy.
// The existing UpcomingEarningsTab already renders the calendar event,
// options market state and historical replay summary from real data; this
// wraps it with the timing policy so the user sees which clock applies.
export function EarningsSetupTab({ ticker, overview, onOverviewChanged }: { ticker: string; overview: ResearchOverview; onOverviewChanged: () => void }) {
  return (
    <div>
      <div className="card" data-testid="timing-policy">
        <SectionHeader title="Decision timing policy" />
        <div className="grid grid-4" style={{ gap: 10 }}>
          <Metric label="V4 decision / entry" value="15:30 ET" mono sub="v4-pre-earnings-1530et-v1 · legal pre-earnings trading day" />
          <Metric label="V3 control decision" value="15:55 ET" mono sub="v3-pre-earnings-1555et-v1 · unchanged" />
          <Metric label="Settlement (both)" value="15:55 ET" mono sub="first post-earnings trading day" />
          <Metric label="Rule" value="AMC: same day · BMO: prior trading day" sub="NYSE holiday calendar applied" />
        </div>
      </div>
      <UpcomingEarningsTab ticker={ticker} overview={overview} onOverviewChanged={onOverviewChanged} />
    </div>
  );
}
