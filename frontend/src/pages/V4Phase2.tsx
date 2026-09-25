import { useMemo, useState } from "react";
import { useAsync } from "../hooks/useAsync";
import { api } from "../api/client";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { configLabel } from "../components/v4/shared";
import type {
  V4Phase2CandidateRow,
  V4Phase2ConfigRow,
  V4Phase2Event,
  V4Phase2RejectionSummary,
} from "../types/api";

// V4.2 PHASE 2 -- INDEPENDENT SEARCH.
//
// The page exists to answer one question an operator cannot answer from the
// Phase-1 surface: did the six configurations actually decide separately?
//
// So the grid always shows all six rows, even when they agree, and the header
// leads with how many DISTINCT structures they chose between them. Phase 1
// never exceeded one on any event, which is invisible if the UI only shows a
// winner. A row that agrees with its neighbours is evidence; a row that is
// hidden because it agreed is not.
//
// NO ACTION reads as an intentional outcome here for the same reason it does
// everywhere else in this product: an absolute viability gate is SUPPOSED to
// produce it, and presenting it as a failure creates pressure to weaken the
// gate.

const STAGE_LABEL: Record<string, string> = {
  data_invalid_count: "Could not be valued",
  strategy_not_permitted_count: "Family not allowed",
  capital_rejected_count: "Above capital base",
  risk_rejected_count: "Above risk cap",
  liquidity_rejected_count: "Below liquidity floor",
  // "Refused on economics", not "failed": in a row whose outcome already
  // reads No action, the word failed makes an intentional refusal look
  // like a system fault -- which is exactly the pressure that gets an
  // absolute viability gate weakened.
  economic_rejected_count: "Refused on economics",
  move_edge_rejected_count: "No move edge",
  rankable_count: "Ranked",
};

// The order the stages actually run in, so the census reads as a funnel
// rather than an alphabetical list.
const STAGE_ORDER = [
  "data_invalid_count",
  "strategy_not_permitted_count",
  "capital_rejected_count",
  "risk_rejected_count",
  "liquidity_rejected_count",
  "economic_rejected_count",
  "move_edge_rejected_count",
  "rankable_count",
] as const;

function pct(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function money(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function strategyLabel(strategy: string): string {
  return strategy.replace(/_/g, " ");
}

function candidateStructure(id: string | null): string {
  if (!id) return "—";
  const [strategy] = id.split(":");
  return strategyLabel(strategy);
}

function candidateExpiry(id: string | null): string {
  if (!id) return "";
  const at = id.lastIndexOf("@");
  return at === -1 ? "" : id.slice(at + 1);
}

function RejectionCensus({ summary }: { summary: V4Phase2RejectionSummary | null }) {
  if (!summary) return <span className="text-faint">—</span>;
  const rows = STAGE_ORDER.map((key) => [key, summary[key] ?? 0] as const).filter(
    ([, count]) => count > 0,
  );
  if (rows.length === 0) return <span className="text-faint">no candidates</span>;
  return (
    <ul className="text-sm" style={{ margin: 0, paddingLeft: "1.1em" }}>
      {rows.map(([key, count]) => (
        <li key={key} className={key === "rankable_count" ? "" : "text-faint"}>
          {STAGE_LABEL[key]}: <span className="mono">{count}</span>
        </li>
      ))}
    </ul>
  );
}

function ConfigurationGrid({ rows }: { rows: V4Phase2ConfigRow[] }) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ fontVariantNumeric: "tabular-nums", width: "100%" }} data-testid="phase2-config-grid">
        <thead>
          <tr>
            <th>Configuration</th>
            <th>Outcome</th>
            <th>Structure</th>
            <th>Expiry</th>
            <th>Qty</th>
            <th>Capital</th>
            <th>At risk</th>
            <th>Cap</th>
            <th>Where its universe went</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.configuration_key} data-testid={`phase2-config-${row.configuration_key}`}>
              <td>{configLabel(row.configuration_key)}</td>
              <td>
                <span className={row.status === "ACTION" ? "pill-positive" : "pill-neutral"}>
                  {row.status === "ACTION" ? "Action" : "No action"}
                </span>
              </td>
              <td>{candidateStructure(row.selected_candidate_id)}</td>
              <td className="mono">{candidateExpiry(row.selected_candidate_id) || "—"}</td>
              <td className="mono">{row.quantity ?? "—"}</td>
              <td className="mono">{money(row.capital_used)}</td>
              <td className="mono">{money(row.max_risk_used)}</td>
              <td className="mono text-faint">{money(row.max_risk_dollars)}</td>
              <td><RejectionCensus summary={row.rejection_summary} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WhyPanel({ rows }: { rows: V4Phase2ConfigRow[] }) {
  return (
    <div className="card" style={{ margin: 0 }}>
      <h3 style={{ marginTop: 0 }}>Why each configuration decided as it did</h3>
      <p className="text-faint text-sm">
        Stated in the configuration&rsquo;s own terms. The raw ranking tuple is not shown
        here &mdash; a reader deciding whether to trust a refusal needs the binding
        constraint, not the sort key.
      </p>
      <dl style={{ margin: 0 }}>
        {rows.map((row) => (
          <div key={row.configuration_key} style={{ marginBottom: 10 }}>
            <dt style={{ fontWeight: 600 }}>{configLabel(row.configuration_key)}</dt>
            <dd className="text-sm" style={{ margin: "2px 0 0 0" }}>
              {row.reason ?? <span className="text-faint">no reason recorded</span>}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function CandidateExplorer({
  candidates,
  configurations,
}: {
  candidates: V4Phase2CandidateRow[];
  configurations: V4Phase2ConfigRow[];
}) {
  const [expiry, setExpiry] = useState("all");
  const [strategy, setStrategy] = useState("all");
  const [rankable, setRankable] = useState("all");
  const [configKey, setConfigKey] = useState("all");

  const expiries = useMemo(
    () => [...new Set(candidates.map((c) => c.expiration).filter(Boolean))].sort(),
    [candidates],
  );
  const strategies = useMemo(
    () => [...new Set(candidates.map((c) => c.strategy))].sort(),
    [candidates],
  );

  // A configuration's own eligible set is the list it actually ranked. Its
  // rejections are everything else -- derived from the same evidence rather
  // than recomputed here, so the explorer can never disagree with the grid.
  const eligibleForConfig = useMemo(() => {
    if (configKey === "all") return null;
    const row = configurations.find((c) => c.configuration_key === configKey);
    return new Set(row?.ranked_candidate_ids ?? []);
  }, [configKey, configurations]);

  const shown = candidates.filter((c) => {
    if (expiry !== "all" && c.expiration !== expiry) return false;
    if (strategy !== "all" && c.strategy !== strategy) return false;
    if (rankable === "rankable" && !c.rankable_somewhere) return false;
    if (rankable === "rejected" && c.rankable_somewhere) return false;
    if (eligibleForConfig && !eligibleForConfig.has(c.candidate_id)) return false;
    return true;
  });

  return (
    <div className="card" style={{ margin: 0 }}>
      <h3 style={{ marginTop: 0 }}>Candidate explorer</h3>
      <p className="text-faint text-sm">
        The complete bounded universe this event searched, refused candidates included.
        Without the refused ones a reader cannot tell a thin field from a strong one.
      </p>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
        <label className="text-sm">
          Expiry{" "}
          <select value={expiry} onChange={(e) => setExpiry(e.target.value)} data-testid="filter-expiry">
            <option value="all">All</option>
            {expiries.map((value) => (
              <option key={value} value={value ?? ""}>{value}</option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          Strategy{" "}
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)} data-testid="filter-strategy">
            <option value="all">All</option>
            {strategies.map((value) => (
              <option key={value} value={value}>{strategyLabel(value)}</option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          Rankability{" "}
          <select value={rankable} onChange={(e) => setRankable(e.target.value)} data-testid="filter-rankable">
            <option value="all">All</option>
            <option value="rankable">Ranked somewhere</option>
            <option value="rejected">Refused everywhere</option>
          </select>
        </label>
        <label className="text-sm">
          Configuration{" "}
          <select value={configKey} onChange={(e) => setConfigKey(e.target.value)} data-testid="filter-config">
            <option value="all">All candidates</option>
            {configurations.map((c) => (
              <option key={c.configuration_key} value={c.configuration_key}>
                {configLabel(c.configuration_key)} eligible set
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="text-sm text-faint">
        Showing <span className="mono">{shown.length}</span> of{" "}
        <span className="mono">{candidates.length}</span>.
      </p>
      <div style={{ overflowX: "auto" }}>
        <table style={{ fontVariantNumeric: "tabular-nums", width: "100%" }} data-testid="phase2-candidates">
          <thead>
            <tr>
              <th>Structure</th>
              <th>Expiry</th>
              <th>Rung</th>
              <th>DTE</th>
              <th>Implied move</th>
              <th>Median T+1</th>
              <th>Worst</th>
              <th>Entry cash</th>
              <th>Max risk</th>
              <th>Spread</th>
              <th>Move edge</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((c) => (
              <tr key={c.candidate_id}>
                <td>{strategyLabel(c.strategy)}</td>
                <td className="mono">{c.expiration ?? "—"}</td>
                <td className="mono">{c.expiry_ladder_position ?? "—"}</td>
                <td className="mono">{c.entry_dte ?? "—"}</td>
                <td className="mono">{pct(c.expiry_implied_move_pct)}</td>
                <td className="mono">{pct(c.core_median_return)}</td>
                <td className="mono">{pct(c.core_worst_return)}</td>
                <td className="mono">{money(c.entry_cash_required)}</td>
                <td className="mono">{money(c.per_contract_max_risk)}</td>
                <td className="mono">{pct(c.mean_relative_spread)}</td>
                <td className="text-sm">{c.move_edge_status ?? "—"}</td>
                <td>
                  <span className={c.rankable_somewhere ? "pill-positive" : "pill-neutral"}>
                    {c.rankable_somewhere ? "Ranked" : c.validity_status ?? "Refused"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {shown.length === 0 && (
        <div className="empty-state" style={{ padding: "12px 0" }}>
          <strong>No candidate matches these filters.</strong>
        </div>
      )}
    </div>
  );
}

function EventCard({ event }: { event: V4Phase2Event }) {
  const detail = useAsync(() => api.getV4Phase2Decision(event.decision_id), [event.decision_id]);
  const distinct = event.distinct_selected_candidates ?? 0;

  return (
    <div className="card" data-testid={`phase2-event-${event.ticker}`}>
      <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ margin: 0 }}>{event.ticker}</h2>
        <div className="text-sm text-faint">
          {event.observed_at?.replace("T", " ").slice(0, 16) ?? "—"}
        </div>
      </div>
      <div style={{ display: "flex", gap: 18, flexWrap: "wrap", margin: "10px 0" }}>
        <div><span className="text-faint text-sm">Expiries searched</span><br /><span className="mono">{event.expiries_considered ?? "—"}</span></div>
        <div><span className="text-faint text-sm">Candidates</span><br /><span className="mono">{event.candidates_evaluated}</span></div>
        <div><span className="text-faint text-sm">Actioned</span><br /><span className="mono">{event.configurations_actioned ?? 0} / 6</span></div>
        <div>
          <span className="text-faint text-sm">Distinct structures</span><br />
          <span className="mono">{distinct}</span>{" "}
          {distinct > 1 && <span className="pill-positive">configurations diverged</span>}
        </div>
        <div><span className="text-faint text-sm">Requests</span><br /><span className="mono">{event.market_data_requests ?? "—"}</span></div>
        <div><span className="text-faint text-sm">Unique contracts</span><br /><span className="mono">{event.unique_contracts_quoted ?? "—"}</span></div>
        <div>
          <span className="text-faint text-sm">Latency</span><br />
          <span className="mono">
            {event.total_latency_ms ? `${Math.round(Number(event.total_latency_ms))} ms` : "—"}
          </span>
        </div>
      </div>
      <ConfigurationGrid rows={event.configurations} />
      <div style={{ display: "grid", gap: 16, marginTop: 16 }}>
        <WhyPanel rows={event.configurations} />
        {detail.data && (
          <CandidateExplorer
            candidates={detail.data.candidates}
            configurations={detail.data.configurations}
          />
        )}
        {detail.loading && !detail.data && <LoadingState label="Loading the candidate universe…" />}
      </div>
    </div>
  );
}

export function V4Phase2() {
  const status = useAsync(() => api.getV4Phase2Status(), []);
  const decisions = useAsync(() => api.getV4Phase2Decisions(), []);

  if (status.loading && !status.data) return <LoadingState label="Loading Phase 2…" />;
  if (status.error && !status.data) return <ErrorState message={status.error} />;
  if (!status.data) return null;

  const s = status.data;
  const events = decisions.data?.events ?? [];

  return (
    <div>
      <h1>V4.2 Phase 2 &mdash; Independent Search</h1>
      <p className="text-faint">{s.notice}</p>

      <div className="card" data-testid="phase2-status">
        <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
          <div>
            <span className="text-faint text-sm">Methodology</span><br />
            <span className="mono">{s.methodology_version}</span>
          </div>
          <div>
            <span className="text-faint text-sm">State</span><br />
            <span className={s.would_evaluate_a_window_now ? "pill-positive" : "pill-neutral"}>
              {s.would_evaluate_a_window_now ? "Active" : "Not active"}
            </span>
          </div>
          <div>
            <span className="text-faint text-sm">Activation instant</span><br />
            <span className="mono">{s.activation_at ?? "never activated"}</span>
          </div>
          <div>
            <span className="text-faint text-sm">Expiry rungs</span><br />
            <span className="mono">{s.max_expiries}</span>
          </div>
        </div>
        {!s.would_evaluate_a_window_now && (
          <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
            {s.activation_state}
          </p>
        )}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>What Phase 2 has recorded</h3>
        <table style={{ fontVariantNumeric: "tabular-nums" }}>
          <tbody>
            <tr><td>Events</td><td className="mono">{s.recorded.decisions}</td></tr>
            <tr><td>Candidates searched</td><td className="mono">{s.recorded.candidates}</td></tr>
            <tr><td>Expiry rungs searched</td><td className="mono">{s.recorded.expiries_searched}</td></tr>
            <tr>
              <td>Configuration results actioned</td>
              <td className="mono">
                {s.recorded.configurations_actioned} / {s.recorded.configuration_results}
              </td>
            </tr>
            <tr>
              <td>Events where the six chose differently</td>
              <td className="mono">{s.recorded.events_with_divergent_selections}</td>
            </tr>
          </tbody>
        </table>
        <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
          The last row is the one that says whether the six are genuinely independent.
          Under Phase 1 it was zero on every event.
        </p>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>The six configurations</h3>
        <div style={{ overflowX: "auto" }}>
          <table style={{ fontVariantNumeric: "tabular-nums", width: "100%" }}>
            <thead>
              <tr>
                <th>Configuration</th>
                <th>Capital</th>
                <th>Risk cap</th>
                <th>Liquidity floor</th>
                <th>Strategy universe</th>
              </tr>
            </thead>
            <tbody>
              {s.configurations.map((c) => (
                <tr key={c.configuration_key}>
                  <td>{c.label}</td>
                  <td className="mono">{money(c.capital_base)}</td>
                  <td className="mono">
                    {money(c.max_risk_dollars)}{" "}
                    <span className="text-faint">({c.max_risk_utilization_pct}%)</span>
                  </td>
                  <td className="mono">{c.min_bid_ask_coverage ?? "none"}</td>
                  <td className="text-sm">
                    {c.risk_profile === "conservative"
                      ? "Defined-risk spreads only (no single-leg longs)"
                      : "All supported defined-risk families"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {events.length === 0 ? (
        <div className="card empty-state" data-testid="phase2-empty">
          <strong>No Phase-2 evidence yet.</strong>
          <p className="text-sm text-faint">
            Phase 2 records only prospectively, from its own activation instant. Nothing here
            is backfilled, so an empty table means the phase has not yet run a live window &mdash;
            not that it found nothing.
          </p>
        </div>
      ) : (
        events.map((event) => <EventCard key={event.decision_id} event={event} />)
      )}
    </div>
  );
}
