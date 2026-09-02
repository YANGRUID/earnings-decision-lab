import { Link } from "react-router-dom";
import { api } from "../../../api/client";
import { useAsync } from "../../../hooks/useAsync";
import { formatMoney, formatRelativeTime } from "../../../lib/format";
import { fmtMarketCap, humanStrategy } from "../../v4/shared";
import { ACTIONABLE_STATUSES } from "../DecisionTab";
import { LifecyclePill, MarketDataQualityBadge, Metric, SectionHeader, Timestamp } from "../../v4/ui";
import type { ResearchOverview } from "../../../types/api";

// Company Overview (Section 3): one screen that answers "where does this
// company stand right now" before any deeper analysis.
export function OverviewV4Tab({ overview, onGo }: { overview: ResearchOverview; onGo: (tab: string) => void }) {
  const ticker = overview.ticker;
  const lab = useAsync(() => api.getStrategyLab(ticker), [ticker]);
  const v4 = useAsync(() => api.getV4ShadowDecisions({ ticker, limit: 1 }), [ticker]);
  const v3 = useAsync(() => api.listDecisionSnapshots({ ticker, limit: 1 }), [ticker]);
  const calendar = useAsync(() => api.getSymbolEarningsCalendar(ticker), [ticker]);
  const cfgs = useAsync(
    () => (v4.data?.decisions[0] ? api.getV4ShadowConfigurations(v4.data.decisions[0].id) : Promise.resolve(null)),
    [v4.data?.decisions[0]?.id],
  );
  const c = overview.company;
  const om = overview.options_market;
  const latestV4 = v4.data?.decisions[0] ?? null;
  const latestV3 = v3.data?.[0] ?? null;
  const defaultCfg = cfgs.data?.configurations.find((x) => x.configuration_key === cfgs.data?.default_configuration_key) ?? null;
  const researchReady = !!overview.latest_job && overview.latest_job.status === "completed" && overview.filings_count > 0;
  const today = new Date().toISOString().slice(0, 10);
  const nextEvent = (calendar.data ?? []).filter((e) => e.earnings_date >= today).sort((a, b) => a.earnings_date.localeCompare(b.earnings_date))[0] ?? (calendar.data ?? [])[0] ?? null;

  return (
    <div>
      <div className="card" data-testid="overview-summary">
        <SectionHeader title={c?.name ?? ticker} eyebrow="Company" right={<span className="mono">{ticker}</span>} />
        <div className="grid grid-4" style={{ gap: 10 }}>
          <Metric label="Market cap" value={fmtMarketCap(nextEvent?.market_cap)} mono />
          <Metric label="Underlying" value={lab.data?.underlying_price ? `$${formatMoney(lab.data.underlying_price)}` : "—"} mono
            sub={lab.data?.snapshot_timestamp ? <Timestamp iso={lab.data.snapshot_timestamp} /> : "no quote observed"} />
          <Metric label="Market data" value={<MarketDataQualityBadge quality={om?.market_data_quality ?? null} provider={lab.data?.snapshot_source ?? null} staleLabel={lab.data?.snapshot_age_label ? `no fresh quote · last ${lab.data.snapshot_age_label}` : undefined} />} />
          <Metric label="Next earnings" value={nextEvent?.earnings_date ?? "—"} mono sub={nextEvent ? `${nextEvent.earnings_time.toUpperCase()} · ${nextEvent.source}` : "no calendar event"} />
        </div>
        <div className="grid grid-4" style={{ gap: 10, marginTop: 10 }}>
          <Metric label="Research" value={<span className={researchReady ? "pill pill-positive" : "pill pill-warning"}>{researchReady ? "prepared" : "not prepared"}</span>}
            sub={overview.latest_job?.completed_at ? `prepared ${formatRelativeTime(overview.latest_job.completed_at)}` : "not prepared yet"} />
          <Metric label="V4 readiness" value={researchReady && om?.actionability && ACTIONABLE_STATUSES.has(om.actionability) ? <span className="pill pill-positive">ready</span> : <span className="pill pill-warning">not ready</span>}
            sub={om?.actionability ? `options: ${String(om.actionability).replace(/_/g, " ").toLowerCase()}` : "options metadata not yet observed"} />
          <Metric label="Latest V4 decision" value={latestV4 ? <LifecyclePill lifecycle={defaultCfg?.lifecycle ?? latestV4.status} /> : <span className="pill pill-neutral">none yet</span>}
            sub={latestV4 ? <Timestamp iso={latestV4.legal_decision_window_at} /> : "V4 shadow has not observed this company"} />
          <Metric label="Latest V3 control" value={latestV3 ? humanStrategy(latestV3.strategy_type) : <span className="text-faint">none</span>}
            sub={latestV3 ? <Timestamp iso={latestV3.generated_at} /> : undefined} />
        </div>
      </div>

      <div className="card">
        <SectionHeader title="Go deeper" />
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          <button className="btn-secondary" onClick={() => onGo("setup")}>Timing &amp; expected move →</button>
          <button className="btn-secondary" onClick={() => onGo("research")}>AI thesis →</button>
          <button className="btn-secondary" onClick={() => onGo("view")}>Direction &amp; confidence →</button>
          <button className="btn-secondary" onClick={() => onGo("decision")}>Six-config recommendation →</button>
          <button className="btn-secondary" onClick={() => onGo("candidates")}>Explore frozen structures →</button>
          <button className="btn-secondary" onClick={() => onGo("outcome")}>Entry &amp; settlement →</button>
          <button className="btn-secondary" onClick={() => onGo("history")}>Price reaction &amp; legacy tools →</button>
          {latestV4 && <Link className="btn-secondary" to={`/same-event-comparison/${latestV4.earnings_calendar_event_id}`}>Same-event comparison →</Link>}
        </div>
      </div>
    </div>
  );
}
