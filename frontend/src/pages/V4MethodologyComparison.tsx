import { useAsync } from "../hooks/useAsync";
import { api } from "../api/client";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { configLabel } from "../components/v4/shared";
import type {
  V4ExpiryRung,
  V4Phase2Status,
  V4MethodologyComparisonEvent,
  V4MethodologySide,
} from "../types/api";

// V4.1 CONTROL vs V4.2 CHALLENGER.
//
// Research surface, deliberately kept out of the primary V4.1 product flow:
// V4.2 is a challenger, not a second product. The language here is CONTROL
// and CHALLENGER throughout -- never "better", "winner" or "improved" --
// because before forward outcomes exist there is nothing to be better at,
// and the comparison's value depends on it not quietly becoming an argument.
//
// Ex-ante evidence, plus the lifecycle state each side reached. A realized
// outcome, when one exists, is shown as its own labelled row with its own
// settlement quality -- never folded into the modeled economics above it, and
// never used to describe one methodology as ahead of the other.

// NO ACTION must read as an intentional, professional outcome. It is what a
// methodology with an absolute viability gate is SUPPOSED to produce when
// nothing clears it, and presenting it as a failure would create pressure to
// weaken the gate.
const LIFECYCLE_PILL: Record<string, string> = {
  NO_ACTION: "pill-neutral",
  PENDING_ENTRY: "pill-neutral",
  ENTRY_FAILED: "pill-warning",
  WAITING_SETTLEMENT: "pill-neutral",
  SETTLED: "pill-positive",
  SETTLEMENT_FAILED: "pill-warning",
};

const LIFECYCLE_LABEL: Record<string, string> = {
  NO_ACTION: "No action",
  PENDING_ENTRY: "Pending entry",
  ENTRY_FAILED: "Entry not executable",
  WAITING_SETTLEMENT: "Awaiting T+1 settlement",
  SETTLED: "Settled",
  SETTLEMENT_FAILED: "Settlement failed",
};

const GRADE_LABEL: Record<string, string> = {
  EXECUTABLE_BID_ASK: "Executable bid/ask",
  MARKET_CLOSE_FALLBACK: "End-of-day closing mark",
  EXPIRATION_INTRINSIC_AT_CLOSE: "Expiration intrinsic value",
};

const READINESS_PILL: Record<string, string> = {
  READY: "pill-positive",
  PARTIAL: "pill-warning",
  MISSING: "pill-neutral",
  AVAILABLE: "pill-positive",
  CANNOT_REPLAY_HONESTLY: "pill-neutral",
};

function pct(v: number | string | null | undefined): string {
  return v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(2)}%`;
}

function SideCard({ side, tone }: { side: V4MethodologySide; tone: "control" | "challenger" }) {
  const acted = side.status === "RANKED";
  return (
    <div className="card" style={{ margin: 0 }} data-testid={`side-${tone}`}>
      <h3 style={{ marginTop: 0 }}>{side.methodology}</h3>
      {side.status === null ? (
        <div className="empty-state" style={{ padding: "12px 0" }}>
          <strong>Not evaluated.</strong> No challenger decision has been frozen for this event.
        </div>
      ) : (
        <>
          <div style={{ marginBottom: 10 }}>
            <span className={`pill ${acted ? "pill-positive" : "pill-neutral"}`}>
              {side.status}
            </span>
          </div>
          {acted ? (
            <table style={{ fontVariantNumeric: "tabular-nums" }}>
              <tbody>
                <tr><td>Strategy</td><td className="mono">{side.strategy ?? "—"}</td></tr>
                <tr><td>Expiration</td><td className="mono">{side.expiration ?? "—"}</td></tr>
                <tr><td>Modeled median T+1</td><td className="mono">{pct(side.median_return)}</td></tr>
                <tr><td>Modeled worst case</td><td className="mono">{pct(side.worst_return)}</td></tr>
                <tr>
                  <td>Positive scenarios</td>
                  <td className="mono">{pct(side.positive_scenario_fraction)}</td>
                </tr>
                {side.move_edge_status ? (
                  <tr>
                    <td>Move edge</td>
                    <td className="mono">
                      {side.move_edge_status}
                      {side.move_edge_ratio !== null && side.move_edge_ratio !== undefined
                        ? ` (${Number(side.move_edge_ratio).toFixed(2)}×)`
                        : ""}
                    </td>
                  </tr>
                ) : null}
                {side.dte_at_settlement !== null && side.dte_at_settlement !== undefined ? (
                  <tr>
                    <td>DTE at entry / settlement</td>
                    <td className="mono">
                      {side.entry_dte ?? "—"} / {side.dte_at_settlement}
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          ) : (
            // The most valuable output a challenger produces: exactly why it
            // declined.
            <div className="notice" data-testid={`no-action-${tone}`}>
              <strong>NO ACTION.</strong>{" "}
              {side.no_action_reason ?? "No reason recorded."}
            </div>
          )}
          {side.lifecycle ? (
            <div style={{ marginTop: 10 }} data-testid={`lifecycle-${tone}`}>
              <span className={`pill ${LIFECYCLE_PILL[side.lifecycle.state] ?? "pill-neutral"}`}>
                {LIFECYCLE_LABEL[side.lifecycle.state] ?? side.lifecycle.state}
              </span>
              {side.lifecycle.settled > 0 ? (
                <p className="text-sm" style={{ marginBottom: 0 }}>
                  Realized P&amp;L{" "}
                  <span className="mono">{side.lifecycle.realized_pnl ?? "—"}</span> across{" "}
                  {side.lifecycle.settled} configuration(s)
                  {side.lifecycle.settlement_grades.length > 0 ? (
                    <>
                      {" "}priced as{" "}
                      {side.lifecycle.settlement_grades
                        .map((grade: string) => GRADE_LABEL[grade] ?? grade)
                        .join(", ")}
                    </>
                  ) : null}
                  .
                </p>
              ) : null}
            </div>
          ) : null}
          <p className="text-faint text-sm" style={{ marginBottom: 0 }}>
            {side.candidates_evaluated ?? 0} candidate(s) evaluated
            {side.candidates_accepted !== null && side.candidates_accepted !== undefined
              ? `, ${side.candidates_accepted} cleared the gate`
              : ""}
            .
          </p>
        </>
      )}
    </div>
  );
}

function ExpiryLadder({ rungs }: { rungs: V4ExpiryRung[] }) {
  return (
    <div data-testid="expiry-ladder">
      <h3>Expiries considered</h3>
      <p className="text-faint text-sm">
        The bounded ladder the challenger compared, each rung on its own listed strikes and
        its own implied move, all valued at the same T+1 objective. Rung 0 is the expiry V4.1
        would have chosen.
      </p>
      <div style={{ overflowX: "auto" }}>
        <table style={{ fontVariantNumeric: "tabular-nums" }}>
          <thead>
            <tr>
              <th>Rung</th><th>Expiration</th><th>DTE at settlement</th>
              <th>Implied move</th><th>Candidates</th><th>Viable</th>
              <th>Best modeled median</th><th>Downside</th><th>Spread</th><th>Move edge</th>
            </tr>
          </thead>
          <tbody>
            {rungs.map((rung) => (
              <tr key={rung.expiration}>
                <td className="mono">{rung.ladder_position ?? "—"}</td>
                <td className="mono">{rung.expiration}</td>
                <td className="mono">
                  {rung.dte_at_settlement ?? "—"}
                  {rung.dte_at_settlement === 0 ? (
                    <span className="pill pill-warning" style={{ marginLeft: 6 }}>
                      expires that day
                    </span>
                  ) : null}
                </td>
                <td className="mono">{pct(rung.implied_move_pct)}</td>
                <td className="mono">{rung.candidates}</td>
                <td className="mono">{rung.viable_candidates}</td>
                <td className="mono">{pct(rung.best_median_return)}</td>
                <td className="mono">{pct(rung.best_worst_return)}</td>
                <td className="mono">{pct(rung.mean_relative_spread)}</td>
                <td className="mono">{rung.move_edge_status ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function EventBlock({ event }: { event: V4MethodologyComparisonEvent }) {
  const evidence = event.challenger_evidence;
  const disagree = event.configurations.filter(
    (c) => c.challenger_status && c.control_status !== c.challenger_status,
  );
  return (
    <div className="card" data-testid={`comparison-${event.ticker}`}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <h2 style={{ margin: 0 }}>{event.ticker}</h2>
        <span className="text-faint text-sm mono">{event.observed_at?.slice(0, 19) ?? ""}</span>
        {event.differs ? <span className="pill pill-warning">METHODOLOGIES DIFFER</span> : null}
      </div>

      <div className="grid grid-2" style={{ gap: 12, marginTop: 12 }}>
        <SideCard side={event.control} tone="control" />
        <SideCard side={event.challenger} tone="challenger" />
      </div>

      {event.multi_expiry.length > 0 ? <ExpiryLadder rungs={event.multi_expiry} /> : null}

      <h3>Challenger evidence readiness</h3>
      <div className="grid grid-4" style={{ gap: 8 }}>
        {(
          [
            ["Historical move", evidence.historical_move],
            ["Timing quality", evidence.historical_timing_quality ?? "—"],
            ["Chain metadata", evidence.multi_expiry_metadata],
            ["Multi-expiry replay", evidence.multi_expiry_replay],
          ] as [string, string][]
        ).map(([label, value]) => (
          <div className="stat" key={label}>
            <span className="stat-label">{label}</span>
            <span className={`pill ${READINESS_PILL[value] ?? "pill-neutral"}`}>{value}</span>
          </div>
        ))}
      </div>
      {evidence.historical_sample_n ? (
        <p className="text-faint text-sm">
          Historical move context: n={evidence.historical_sample_n}, timing{" "}
          {evidence.historical_timing_quality ?? "unknown"}.
        </p>
      ) : null}

      {disagree.length > 0 ? (
        <>
          <h3>Configurations where the two differ</h3>
          <div style={{ overflowX: "auto" }}>
            <table style={{ fontVariantNumeric: "tabular-nums" }}>
              <thead>
                <tr>
                  <th>Configuration</th>
                  <th>Control</th>
                  <th>Challenger</th>
                  <th>Challenger reason</th>
                </tr>
              </thead>
              <tbody>
                {disagree.map((c) => (
                  <tr key={c.configuration_key}>
                    <td>{configLabel(c.configuration_key)}</td>
                    <td className="mono">{c.control_status ?? "—"}</td>
                    <td className="mono">{c.challenger_status ?? "—"}</td>
                    <td className="text-faint text-sm">{c.challenger_no_action_reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </div>
  );
}

// Three methodologies, kept three.
//
// Requirement, and the reason for it: V4.1 Control, V4.2 Phase 1 and V4.2
// Phase 2 ask different questions of the same window, so their evidence is
// three series and not one. Phase 2 starts at N=0 and stays there until it has
// run live windows of its own -- which is stated here rather than left to be
// inferred from an empty table, because an empty table reads as "found
// nothing" and the truth is "has not run".
function MethodologyBoundaries({ phase2 }: { phase2: V4Phase2Status | null }) {
  const rows: [string, string, string, string][] = [
    [
      "V4.1 Control",
      "The official methodology",
      "One expiry, chosen by V4.1; candidates screened at the $2,000 standardized capital",
      "Six configurations already select independently within that universe",
    ],
    [
      "V4.2 Phase 1",
      "Shared-candidate challenger",
      "Reads the control's own frozen shortlist; no additional market data",
      "An absolute viability gate over that shortlist, identical for all six configurations",
    ],
    [
      "V4.2 Phase 2",
      "Independent search",
      "Its own bounded multi-expiry universe, each rung with its own implied move",
      "Each configuration applies its own family, liquidity, capital and risk rules, then ranks",
    ],
  ];
  return (
    <div className="card" data-testid="methodology-boundaries">
      <h3 style={{ marginTop: 0 }}>Three methodologies, not one series</h3>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%" }}>
          <thead>
            <tr>
              <th>Methodology</th>
              <th>What it is</th>
              <th>Candidate universe</th>
              <th>Selection</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([name, what, universe, selection]) => (
              <tr key={name}>
                <td><strong>{name}</strong></td>
                <td className="text-sm">{what}</td>
                <td className="text-sm">{universe}</td>
                <td className="text-sm">{selection}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-faint text-sm" style={{ marginBottom: 0 }}>
        {phase2
          ? phase2.recorded.decisions === 0
            ? `Phase 2 has recorded no forward events (N = 0). It is ${phase2.would_evaluate_a_window_now ? "active" : "not active"} and records only prospectively, from its own activation instant — nothing is backfilled. No claim that Phase 2 is better or worse than either of the others can be made until it has forward data of its own.`
            : `Phase 2 has recorded ${phase2.recorded.decisions} forward event(s), with the six configurations choosing differently on ${phase2.recorded.events_with_divergent_selections} of them. Far too few to rank the three methodologies against each other.`
          : "Phase 2 status is unavailable on this request."}
      </p>
    </div>
  );
}

export function V4MethodologyComparison() {
  const comparison = useAsync(() => api.getV4MethodologyComparison(), []);
  const phase2 = useAsync(() => api.getV4Phase2Status(), []);

  if (comparison.loading && !comparison.data) {
    return <LoadingState label="Loading methodology comparison…" />;
  }
  if (comparison.error && !comparison.data) return <ErrorState message={comparison.error} />;
  if (!comparison.data) return null;
  const { events, counts, notice } = comparison.data;

  return (
    <div>
      <div className="page-header"><h1>Methodology Comparison</h1></div>
      <div className="notice notice-warning" data-testid="challenger-notice">{notice}</div>

      <MethodologyBoundaries phase2={phase2.data ?? null} />

      <div className="card">
        <div className="grid grid-3" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">Events</span>
            <span className="stat-value mono">{counts.events}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Challenger evaluated</span>
            <span className="stat-value mono">{counts.challenger_evaluated}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Methodologies differ</span>
            <span className="stat-value mono">{counts.differs}</span>
          </div>
        </div>
        <p className="text-faint text-sm" style={{ marginBottom: 0 }}>
          The comparison unit is the <strong>event</strong>. The six configurations are sizing
          variants of one market view, not six independent forecasts, so they are reported beneath
          an event rather than beside it. Everything here is pre-outcome evidence.
        </p>
      </div>

      {events.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <strong>No V4 events yet.</strong>
          </div>
        </div>
      ) : (
        events.map((event) => <EventBlock key={event.ticker} event={event} />)
      )}
    </div>
  );
}
