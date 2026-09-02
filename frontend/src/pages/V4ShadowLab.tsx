import { useState } from "react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import type { V4ShadowCandidate, V4ShadowDecisionDetail } from "../types/api";

/**
 * V4.5 -- V4 Shadow Lab (Sections 57-62).
 *
 * A deliberately SEPARATE surface from the official AI Decision and
 * Benchmark Track Record pages. Nothing here is official forward-test
 * evidence, and every screen says so prominently (Section 58): a reader
 * must never be able to mistake an experimental shadow observation for a
 * real benchmark record.
 *
 * Read-only by construction. There is no force-entry, force-settlement,
 * or override-ranking control anywhere (Section 69) -- the scheduler owns
 * the real shadow lifecycle.
 */

export function ExperimentalBanner() {
  return (
    <div
      className="notice"
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 10,
        alignItems: "center",
        borderLeft: "3px solid var(--color-warning-text)",
      }}
    >
      <strong style={{ letterSpacing: ".04em" }}>EXPERIMENTAL SHADOW</strong>
      <span className="text-muted">·</span>
      <span>NOT OFFICIAL FORWARD TEST</span>
      <span className="text-muted">·</span>
      <span>NO BROKERAGE ORDER</span>
    </div>
  );
}

function fmt(value: string | null | undefined, digits = 4): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : String(value);
}

function pct(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : String(value);
}

function CandidateRow({ candidate }: { candidate: V4ShadowCandidate }) {
  const [open, setOpen] = useState(false);
  const rankable = candidate.rank !== null;
  return (
    <>
      <tr onClick={() => setOpen((o) => !o)} style={{ cursor: "pointer" }}>
        <td className="mono">{candidate.rank ?? "—"}</td>
        <td>
          <span className={`pill ${rankable ? "pill-positive" : "pill-neutral"}`}>
            {candidate.validity_status}
          </span>
        </td>
        <td>{candidate.strategy}</td>
        <td className="mono">{candidate.expiration}</td>
        <td className="mono">{fmt(candidate.semantic?.compatibility, 2)}</td>
        <td className="mono">{fmt(candidate.core?.worst_return)}</td>
        <td className="mono">{fmt(candidate.core?.median_return)}</td>
        <td className="mono">{pct(candidate.core?.positive_scenario_fraction)}</td>
        <td className="mono">{fmt(candidate.tail_stress?.worst_return)}</td>
        <td className="mono">{pct(candidate.execution?.mean_relative_spread)}</td>
        <td className="mono">{pct(candidate.capital?.capital_utilisation)}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={11} style={{ background: "var(--color-bg)" }}>
            <div style={{ padding: "10px 4px" }}>
              {candidate.rank_explanation && (
                <p className="text-sm" style={{ marginTop: 0 }}>
                  <strong>Why this rank:</strong> {candidate.rank_explanation}
                </p>
              )}
              {candidate.core?.no_profitable_region && (
                <p className="text-sm negative">
                  Not profitable in ANY modeled underlying-move region.
                </p>
              )}
              {candidate.core?.profit_concentrated_in_single_region && (
                <p className="text-sm warning">
                  Profit concentrated in a single price region — depends on the underlying
                  pinning there.
                </p>
              )}
              <div style={{ overflowX: "auto" }}>
                <table>
                  <thead>
                    <tr>
                      <th>Leg</th>
                      <th>Side</th>
                      <th>Strike</th>
                      <th>Required side</th>
                      <th>Price</th>
                      <th>Bid</th>
                      <th>Ask</th>
                      <th>IV</th>
                      <th>conId</th>
                      <th>Quality</th>
                      <th>Quote time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {candidate.legs.map((leg) => (
                      <tr key={leg.leg_index}>
                        <td className="mono">{leg.leg_index}</td>
                        <td>
                          {leg.action} {leg.right}
                        </td>
                        <td className="mono">{fmt(leg.strike, 2)}</td>
                        <td className="mono">{leg.required_side ?? "—"}</td>
                        <td className="mono">{fmt(leg.required_side_price, 2)}</td>
                        <td className="mono">{fmt(leg.bid, 2)}</td>
                        <td className="mono">{fmt(leg.ask, 2)}</td>
                        <td className="mono">{fmt(leg.implied_volatility, 3)}</td>
                        <td className="mono">{leg.external_contract_id ?? "—"}</td>
                        <td className="mono">{leg.market_data_quality ?? "—"}</td>
                        <td className="mono text-sm">
                          {leg.retrieved_at ? new Date(leg.retrieved_at).toLocaleTimeString() : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="grid grid-4" style={{ gap: 10, marginTop: 10 }}>
                <div className="stat">
                  <span className="stat-label">Stress survival</span>
                  <span className="stat-value small">
                    {pct(candidate.tail_stress?.large_move_survival)}
                  </span>
                </div>
                <div className="stat">
                  <span className="stat-label">Cross-leg skew</span>
                  <span className="stat-value small mono">
                    {candidate.execution?.max_leg_timestamp_skew_seconds ?? "—"}s
                  </span>
                </div>
                <div className="stat">
                  <span className="stat-label">Entry cash</span>
                  <span className="stat-value small mono">
                    {fmt(candidate.capital?.entry_cash_required, 2)}
                  </span>
                </div>
                <div className="stat">
                  <span className="stat-label">Two-sided legs</span>
                  <span className="stat-value small mono">
                    {candidate.execution?.two_sided_leg_count ?? "—"}/
                    {candidate.execution?.leg_count ?? "—"}
                  </span>
                </div>
              </div>
              {candidate.tail_stress?.note && (
                <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                  {candidate.tail_stress.note}
                </p>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function DecisionDetail({ decisionId, onBack }: { decisionId: number; onBack: () => void }) {
  const detail = useAsync<V4ShadowDecisionDetail>(
    () => api.getV4ShadowDecision(decisionId),
    [decisionId]
  );
  const candidates = useAsync(() => api.getV4ShadowCandidates(decisionId), [decisionId]);

  if (detail.loading && !detail.data) return <LoadingState label="Loading shadow decision…" />;
  if (detail.error && !detail.data) return <ErrorState message={detail.error} />;
  if (!detail.data) return null;
  const d = detail.data;

  return (
    <div>
      <button className="btn-secondary" onClick={onBack} style={{ marginBottom: 14 }}>
        ← All shadow decisions
      </button>
      <ExperimentalBanner />
      <div className="card">
        <h2>
          {d.ticker} — shadow decision #{d.id}
        </h2>
        <div className="grid grid-4" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">Status</span>
            <span className="stat-value small">{d.status}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Legal window</span>
            <span className="stat-value small mono">
              {new Date(d.legal_decision_window_at).toLocaleString()}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Rank #1</span>
            <span className="stat-value small mono">{d.rank_1_candidate_id ?? "—"}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Candidates</span>
            <span className="stat-value small mono">
              {d.rankable_candidate_count}/{d.candidate_count} rankable
            </span>
          </div>
        </div>
        {d.no_action_reason && (
          <div className="notice" style={{ marginTop: 12 }}>
            NO_ACTION — {d.no_action_reason}
          </div>
        )}
      </div>

      <div className="card">
        <h2>Decision view</h2>
        <div className="grid grid-4" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">Direction</span>
            <span className="stat-value small">{d.view?.direction ?? "—"}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Volatility view</span>
            <span className="stat-value small">{d.view?.volatility ?? "—"}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Move intent</span>
            <span className="stat-value small">{d.view?.expected_move_intent ?? "—"}</span>
          </div>
          <div className="stat">
            <span className="stat-label">View confidence</span>
            <span className="stat-value small">{d.view?.confidence ?? "—"}</span>
          </div>
        </div>
        {d.view?.reasoning && (
          <p className="text-sm" style={{ marginBottom: 0 }}>
            {d.view.reasoning}
          </p>
        )}
        <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
          {d.provenance?.llm_provider ?? "—"} / {d.provenance?.llm_model ?? "—"} · prompt{" "}
          {d.provenance?.prompt_version ?? "—"} · view schema{" "}
          {d.provenance?.decision_view_schema_version ?? "—"}
        </p>
      </div>

      <div className="card">
        <h2>Market data</h2>
        <div className="grid grid-4" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">Provider</span>
            <span className="stat-value small mono">
              {d.market_data?.source_provider ?? "—"}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Quality</span>
            <span className="stat-value small mono">
              {d.market_data?.market_data_quality ?? "—"}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Underlying</span>
            <span className="stat-value small mono">
              {fmt(d.market_data?.underlying_price, 2)}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Max input skew</span>
            <span className="stat-value small mono">
              {d.market_data?.max_input_skew_seconds ?? "—"}s
            </span>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Ranked candidates</h2>
        <p className="text-sm text-muted" style={{ marginTop: 0 }}>
          The complete frozen candidate set — not only rank #1. Click any row for per-leg
          point-in-time evidence. CORE and TAIL STRESS are separate measurements and are never
          averaged together.
        </p>
        {candidates.data ? (
          <div style={{ overflowX: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Validity</th>
                  <th>Strategy</th>
                  <th>Expiration</th>
                  <th>Semantic</th>
                  <th>Core worst</th>
                  <th>Core median</th>
                  <th>Core coverage</th>
                  <th>Stress worst</th>
                  <th>Spread</th>
                  <th>Capital</th>
                </tr>
              </thead>
              <tbody>
                {candidates.data.candidates.map((c) => (
                  <CandidateRow key={c.candidate_id} candidate={c} />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <LoadingState label="Loading candidates…" />
        )}
      </div>

      <div className="card">
        <h2>Methodology versions</h2>
        <div className="grid grid-4" style={{ gap: 10 }}>
          {Object.entries(d.versions ?? {}).map(([key, value]) => (
            <div className="stat" key={key}>
              <span className="stat-label">{key.replace(/_/g, " ")}</span>
              <span className="stat-value small mono">{String(value ?? "—")}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function V4ShadowLab() {
  const [selected, setSelected] = useState<number | null>(null);
  const decisions = useAsync(() => api.getV4ShadowDecisions(), []);

  if (selected !== null) {
    return <DecisionDetail decisionId={selected} onBack={() => setSelected(null)} />;
  }

  if (decisions.loading && !decisions.data) return <LoadingState label="Loading shadow lab…" />;
  if (decisions.error && !decisions.data) return <ErrorState message={decisions.error} />;

  const rows = decisions.data?.decisions ?? [];

  return (
    <div>
      <div className="page-header">
        <h1>V4 Shadow Lab</h1>
        <p>
          What V4 <em>would</em> have chosen, at the same legal decision window V3 used, from the
          same point-in-time market state. V3 remains the official forward-test engine; nothing
          here places an order or affects official execution.
        </p>
      </div>
      <ExperimentalBanner />

      {rows.length === 0 ? (
        <div className="card">
          <h2>No shadow decisions yet</h2>
          <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
            The shadow cohort has produced no decisions. If the cohort is disabled, that is
            expected — shadow evidence is created only by a natural scheduler run at the legal
            decision window after activation, and is never backfilled for past events.
          </p>
        </div>
      ) : (
        <div className="card">
          <div style={{ overflowX: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Legal window</th>
                  <th>Status</th>
                  <th>Direction</th>
                  <th>Volatility</th>
                  <th>Candidates</th>
                  <th>Rank #1</th>
                  <th>Market data</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((d) => (
                  <tr
                    key={d.id}
                    onClick={() => setSelected(d.id)}
                    style={{ cursor: "pointer" }}
                  >
                    <td className="mono">{d.ticker}</td>
                    <td className="mono text-sm">
                      {new Date(d.legal_decision_window_at).toLocaleString()}
                    </td>
                    <td>
                      <span
                        className={`pill ${
                          d.status === "RANKED"
                            ? "pill-positive"
                            : d.status === "NO_ACTION"
                              ? "pill-neutral"
                              : "pill-negative"
                        }`}
                      >
                        {d.status}
                      </span>
                    </td>
                    <td>{d.view?.direction ?? "—"}</td>
                    <td>{d.view?.volatility ?? "—"}</td>
                    <td className="mono">
                      {d.rankable_candidate_count}/{d.candidate_count}
                    </td>
                    <td className="mono text-sm">{d.rank_1_candidate_id ?? "—"}</td>
                    <td className="mono text-sm">{d.market_data?.market_data_quality ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
