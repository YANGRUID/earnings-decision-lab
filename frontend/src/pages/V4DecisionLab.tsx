import { useMemo, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { useListControls } from "../hooks/useListControls";
import { ListToolbar, Pager } from "../components/ListControls";
import { EmptyState, ErrorState, LoadingState } from "../components/StatusStates";
import { CONFIG_ORDER, fmtIv, fmtStrike, humanReasonCode, humanStatus, humanStrategy, money, pct, statusPill } from "../components/v4/shared";
import { ExperimentalNotice, MethodologyDetails } from "../components/v4/sharedComponents";
import { FailureExplanation, LifecyclePill, MarketDataQualityBadge, Metric, SectionHeader, Timestamp } from "../components/v4/ui";
import type { V4EntryObservation, V4SettlementOutcome } from "../types/api";
import type {
  V4CandidateSummary, V4ConfigResult, V4ExpectedMove, V4ScenarioCell, V4ShadowCandidate,
  V4ShadowConfigurationsResponse, V4ShadowDecisionSummary,
} from "../types/api";

// ---------------------------------------------------------------------------
// V4 Decision Lab -- the primary decision surface (V4 consolidation,
// Sections 20-27). One event-level evidence freeze, six configuration
// results, switched client-side without any further model or market-data
// request. Everything rendered is frozen evidence; nothing is recomputed.
// ---------------------------------------------------------------------------

function DecisionPicker({ onPick, mode }: { onPick: (id: number) => void; mode: "lab" | "explorer" }) {
  const list = useAsync(() => api.getV4ShadowDecisions(), []);
  const rows = list.data?.decisions ?? [];
  // Every forward-test day adds events here; the list stays searchable,
  // filterable by status and paged (?v4_*), with "All" always available.
  const controls = useListControls({
    rows,
    urlKey: "v4",
    searchKeys: [(d) => d.ticker, (d) => d.company_name],
    facet: { label: "Status", getValue: (d) => d.status, format: (v) => v.replace(/_/g, " ") },
    defaultPageSize: 25,
  });
  if (list.loading && !list.data) return <LoadingState label="Loading V4 decisions…" />;
  if (list.error && !list.data) return <ErrorState message={list.error} />;
  if (rows.length === 0) {
    return (
      <EmptyState>
        <strong>No V4 decisions yet.</strong> The V4 shadow engine has not produced a forward
        observation. The first natural run happens at 15:30 ET on the next legal earnings day
        after activation; nothing is back-filled and nothing here is simulated.
      </EmptyState>
    );
  }
  return (
    <div className="card">
      <h2>{mode === "explorer" ? "Choose a decision to explore its candidates" : "V4 decisions"}</h2>
      <ListToolbar controls={controls} placeholder="Search ticker or company" testId="v4-decisions-controls" />
      <table>
        <thead>
          <tr>
            <th>Ticker</th><th>Company</th><th>Observed (ET)</th><th>Status</th>
            <th className="mono" style={{ textAlign: "right" }}>Candidates</th><th></th>
          </tr>
        </thead>
        <tbody>
          {controls.visible.map((d) => {
            const pill = statusPill(d.status);
            return (
              <tr key={d.id}>
                <td className="mono"><strong>{d.ticker}</strong></td>
                <td>{d.company_name}</td>
                <td className="mono">
                  {new Date(d.legal_decision_window_at).toLocaleString("en-US", {
                    timeZone: "America/New_York", month: "short", day: "numeric",
                    hour: "2-digit", minute: "2-digit",
                  })}
                </td>
                <td><span className={pill.className}>{pill.label}</span></td>
                <td className="mono" style={{ textAlign: "right" }}>
                  {d.rankable_candidate_count}/{d.candidate_count}
                </td>
                <td><button className="btn-secondary" onClick={() => onPick(d.id)}>Open</button></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <Pager controls={controls} testId="v4-decisions-pager" />
    </div>
  );
}

// --- Hero -----------------------------------------------------------------
function Hero({ d, cfg, timing }: {
  d: V4ShadowDecisionSummary & { expected_move?: V4ExpectedMove | null };
  cfg: V4ConfigResult | null; timing: string | null;
}) {
  const top = cfg?.rank_1 ?? null;
  const md = d.market_data;
  const em = d.expected_move;
  const moveView = d.view?.expected_move_intent;
  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
        <h1 style={{ margin: 0 }}>
          <span className="mono">{d.ticker}</span> <span className="text-muted">— {d.company_name}</span>
        </h1>
        <div className="text-sm text-muted">
          Observed {new Date(d.legal_decision_window_at).toLocaleString("en-US", { timeZone: "America/New_York" })} ET
          {timing ? <> · <span className="mono">{timing}</span></> : null}
        </div>
      </div>

      <div className="grid grid-4" style={{ gap: 10, marginTop: 12 }}>
        <div className="stat">
          <span className="stat-label">Underlying</span>
          <span className="stat-value">{money(md?.underlying_price, 2)}</span>
          <span className="text-faint text-sm">
            {md?.underlying_quote_at ? new Date(md.underlying_quote_at).toLocaleTimeString("en-US", { timeZone: "America/New_York" }) + " ET" : "—"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Market view</span>
          <span className="stat-value small">{(d.view?.direction ?? "—").toUpperCase()}</span>
          <span className="text-faint text-sm">confidence: {d.view?.confidence ?? "—"} (not a probability)</span>
        </div>
        <div className="stat">
          <span className="stat-label">Move view</span>
          <span className="stat-value small">{moveView ? moveView.replace(/_/g, " ").toUpperCase() : "—"}</span>
          <span className="text-faint text-sm">{d.view?.volatility ?? "—"}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Expected move</span>
          <span className="stat-value small">{em?.implied_move_available ? `±${pct(em.implied_move_pct)}` : "unavailable"}</span>
          <span className="text-faint text-sm">hist. median {pct(em?.historical_median_abs_move_pct)} (n={em?.historical_sample_n ?? "—"})</span>
        </div>
      </div>

      <div className="grid grid-4" style={{ gap: 10, marginTop: 10 }}>
        <div className="stat">
          <span className="stat-label">Configuration</span>
          <span className="stat-value small">{cfg ? cfg.label : "—"}</span>
          <span className="text-faint text-sm">max risk {cfg ? money(cfg.max_risk_dollars) : "—"} ({cfg?.max_risk_utilization_pct ?? "—"}%)</span>
        </div>
        <div className="stat">
          <span className="stat-label">Recommended strategy</span>
          <span className="stat-value small">{cfg?.status === "RANKED" ? humanStrategy(top?.strategy) : humanStatus(cfg?.status)}</span>
          <span className="text-faint text-sm mono">{top?.expiration ? `exp ${top.expiration}` : ""}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Capital required</span>
          <span className="stat-value small">{money(top?.entry_cash_required)}</span>
          <span className="text-faint text-sm">defined risk within {cfg ? money(cfg.max_risk_dollars) : "—"}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Market data</span>
          <span className="stat-value small">
            {md?.source_provider === "ibkr_tws" ? "TWS" : md?.source_provider ?? "—"} · {(md?.market_data_quality ?? "—").toUpperCase()}
          </span>
          <span className="text-faint text-sm">
            {md?.market_data_quality?.toLowerCase() === "delayed" ? "delayed data, labelled as such" : ""}
          </span>
        </div>
      </div>
      {cfg?.status === "NO_ACTION" && cfg.no_action_reason && (
        <div className="notice" style={{ marginTop: 12 }}>
          <strong>No action for {cfg.label}.</strong> {cfg.no_action_reason}
        </div>
      )}
      <MethodologyDetails versions={{
        ...(d.versions ?? {}),
        timing_policy: timing,
        configuration: cfg?.configuration_version ?? null,
        decision_model: d.provenance?.llm_model ? `${d.provenance.llm_provider ?? ""} ${d.provenance.llm_model}`.trim() : null,
        decision_reasoning: d.provenance?.llm_thinking
          ? `thinking ${d.provenance.llm_thinking}${d.provenance.llm_reasoning_effort ? ` · effort ${d.provenance.llm_reasoning_effort}` : ""}`
          : null,
        model_returned: d.provenance?.llm_returned_model ?? null,
        prompt: d.provenance?.prompt_version ?? null,
      }} />
    </div>
  );
}

// --- Six-config selector + comparison -------------------------------------
function ConfigSelector({ configs, selected, onSelect }: {
  configs: V4ConfigResult[]; selected: string; onSelect: (k: string) => void;
}) {
  const [cap, risk] = selected.split("_").slice(1);
  const pick = (c: string, r: string) => onSelect(`v4_${c}_${r}`);
  const has = (k: string) => configs.some((c) => c.configuration_key === k);
  return (
    <div className="card" data-testid="config-selector">
      <h2>Configuration</h2>
      <p className="text-muted text-sm">
        Switch between the six frozen results. No model call, no market-data request — every
        configuration was evaluated once, on the same evidence, when this decision was frozen.
      </p>
      <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
        <div>
          <div className="stat-label">Capital</div>
          <div className="tab-bar">
            {["2k", "10k"].map((c) => (
              <button key={c} className={`tab-button ${cap === c ? "active" : ""}`}
                disabled={!has(`v4_${c}_${risk}`)} onClick={() => pick(c, risk)}>
                {c === "2k" ? "$2,000" : "$10,000"}
              </button>
            ))}
          </div>
        </div>
        <div>
          <div className="stat-label">Risk profile</div>
          <div className="tab-bar">
            {["conservative", "moderate", "aggressive"].map((r) => (
              <button key={r} className={`tab-button ${risk === r ? "active" : ""}`}
                disabled={!has(`v4_${cap}_${r}`)} onClick={() => pick(cap, r)}>
                {r[0].toUpperCase() + r.slice(1)}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function ConfigComparison({ configs, selected, onSelect }: {
  configs: V4ConfigResult[]; selected: string; onSelect: (k: string) => void;
}) {
  return (
    <div className="card" data-testid="config-comparison">
      <h2>All six configurations — same evidence</h2>
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Configuration</th><th>Action</th><th>Lifecycle</th><th>Strategy</th>
              <th style={{ textAlign: "right" }}>Capital used</th>
              <th style={{ textAlign: "right" }}>Max risk</th>
              <th style={{ textAlign: "right" }}>Core median</th>
              <th style={{ textAlign: "right" }}>Core worst</th>
              <th style={{ textAlign: "right" }}>Positive coverage</th>
              <th style={{ textAlign: "right" }}>Stress worst</th>
              <th style={{ textAlign: "right" }}>Spread</th>
              <th>Rank #1</th>
            </tr>
          </thead>
          <tbody>
            {CONFIG_ORDER.map((key) => {
              const c = configs.find((x) => x.configuration_key === key);
              if (!c) return null;
              const t = c.rank_1;
              const pill = statusPill(c.status);
              return (
                <tr key={key} onClick={() => onSelect(key)}
                  style={{ cursor: "pointer", background: key === selected ? "var(--accent-soft, rgba(90,168,189,.12))" : undefined }}>
                  <td><strong>{c.label}</strong></td>
                  <td><span className={pill.className}>{pill.label}</span></td>
                  <td><LifecyclePill lifecycle={c.lifecycle} /></td>
                  <td>{t ? humanStrategy(t.strategy) : <span className="text-faint">—</span>}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{money(t?.entry_cash_required)}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{money(c.max_risk_dollars)}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{pct(t?.core_median_return)}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{pct(t?.core_worst_return)}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{pct(t?.core_positive_scenario_fraction, 0)}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{pct(t?.stress_worst_return)}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{pct(t?.mean_relative_spread)}</td>
                  <td className="mono text-sm">{c.rank_1_candidate_id ?? "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-faint text-sm" style={{ marginTop: 8 }}>
        Positive coverage is a count of deterministic scenarios, not a probability. Stress
        points are never averaged into core statistics.
      </p>
    </div>
  );
}

// --- Why this strategy (deterministic, from ranking evidence) ------------
function WhyThisStrategy({ cfg, candidates }: { cfg: V4ConfigResult; candidates: V4CandidateSummary[] }) {
  if (cfg.status !== "RANKED" || !cfg.rank_1) return null;
  const top = cfg.rank_1;
  const ordered = cfg.ranked_candidate_ids
    .map((id) => candidates.find((c) => c.candidate_id === id))
    .filter((c): c is V4CandidateSummary => !!c);
  const runners = ordered.slice(1, 4);
  const num = (v: string | null) => (v == null ? null : Number(v));

  const checks: string[] = [];
  if (top.semantic_tier) checks.push(`Compatible with the ${top.semantic_tier.replace(/_/g, " ")} market view`);
  if (num(top.core_worst_return) != null) checks.push(`Core worst-case ${pct(top.core_worst_return)} — best downside band among eligible candidates`);
  if (num(top.core_median_return) != null) checks.push(`Modeled T+1 median ${pct(top.core_median_return)}`);
  if (num(top.core_positive_scenario_fraction) != null) checks.push(`${pct(top.core_positive_scenario_fraction, 0)} of core scenarios non-negative`);
  if (num(top.mean_relative_spread) != null) checks.push(`Executable spread ${pct(top.mean_relative_spread)} — acceptable`);
  checks.push(`Fits ${cfg.label}: ${money(top.entry_cash_required)} within ${money(cfg.max_risk_dollars)} max risk`);

  const whyNot = (r: V4CandidateSummary): string => {
    const reasons: string[] = [];
    const a = num(r.core_worst_return), b = num(top.core_worst_return);
    if (a != null && b != null && a < b) reasons.push("worse downside band");
    const m1 = num(r.core_median_return), m2 = num(top.core_median_return);
    if (m1 != null && m2 != null && m1 < m2) reasons.push("weaker T+1 median economics");
    const s1 = num(r.mean_relative_spread), s2 = num(top.mean_relative_spread);
    if (s1 != null && s2 != null && s1 > s2) reasons.push("wider execution spread");
    const p1 = num(r.core_positive_scenario_fraction), p2 = num(top.core_positive_scenario_fraction);
    if (p1 != null && p2 != null && p1 < p2) reasons.push("less positive-scenario coverage");
    if (r.semantic_tier && top.semantic_tier && r.semantic_tier !== top.semantic_tier) reasons.push("weaker semantic fit");
    return reasons.length ? reasons.join("; ") : "ranked lower on deterministic tie-break";
  };

  return (
    <div className="card" data-testid="why-this-strategy">
      <h2>Why this strategy ranked first</h2>
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {checks.map((c, i) => <li key={i} className="positive">✓ {c}</li>)}
      </ul>
      {top.rank_explanation && (
        <p className="text-muted text-sm" style={{ marginTop: 8 }}>{top.rank_explanation}</p>
      )}
      {runners.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>Why not the alternatives</h3>
          <table>
            <tbody>
              {runners.map((r, i) => (
                <tr key={r.candidate_id}>
                  <td className="mono">#{i + 2}</td>
                  <td>{humanStrategy(r.strategy)} <span className="text-faint mono text-sm">{r.expiration}</span></td>
                  <td className="text-muted">{whyNot(r)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {cfg.exclusions.length > 0 && (
        <details style={{ marginTop: 10 }}>
          <summary className="text-muted text-sm">{cfg.exclusions.length} candidate(s) not eligible for {cfg.label}</summary>
          <table style={{ marginTop: 6, fontSize: ".85rem" }}>
            <tbody>
              {cfg.exclusions.map((e) => (
                <tr key={e.candidate_id}>
                  <td className="mono">{e.candidate_id}</td>
                  <td><span className="pill pill-warning">{humanReasonCode(e.reason_code)}</span></td>
                  <td className="text-muted">{e.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
      <p className="text-faint text-sm" style={{ marginTop: 8 }}>
        Deterministic, from the frozen V4 ranking evidence — not an LLM explanation.
      </p>
    </div>
  );
}

// --- Expected-move visualization (SVG, real frozen data only) -------------
function ExpectedMoveChart({ em, spot, candidates, highlightId }: {
  em: V4ExpectedMove | null | undefined; spot: string | null | undefined;
  candidates: V4ShadowCandidate[]; highlightId: string | null;
}) {
  if (!em || !spot) return null;
  const S = Number(spot);
  const implied = em.implied_move_pct != null ? Number(em.implied_move_pct) : null;
  const hist = em.historical_median_abs_move_pct != null ? Number(em.historical_median_abs_move_pct) : null;
  if (!S || Number.isNaN(S)) return null;
  const span = Math.max((implied ?? hist ?? 0.05) * 2.4, 0.06);
  const lo = S * (1 - span), hi = S * (1 + span);
  const W = 720, H = 150, padL = 24, padR = 24;
  const x = (p: number) => padL + ((p - lo) / (hi - lo)) * (W - padL - padR);
  const bands = implied != null ? [
    { k: 1.0, label: "±1 EM", dash: "" }, { k: 1.5, label: "±1.5 EM", dash: "4 3" }, { k: 2.0, label: "±2 EM", dash: "2 3" },
  ] : [];
  const strikes: { strike: number; right: string; action: string; cid: string }[] = [];
  for (const c of candidates) for (const l of c.legs) strikes.push({ strike: Number(l.strike), right: l.right, action: l.action, cid: c.candidate_id });
  return (
    <div className="card" data-testid="expected-move-chart">
      <h2>Expected move &amp; strike geometry</h2>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Expected move bands with candidate strikes" style={{ width: "100%", height: "auto" }}>
        <line x1={padL} x2={W - padR} y1={90} y2={90} stroke="currentColor" strokeWidth="1" opacity=".35" />
        {bands.map((b) => {
          const l = x(S * (1 - implied! * b.k)), r = x(S * (1 + implied! * b.k));
          return (
            <g key={b.k}>
              <rect x={l} y={62} width={r - l} height={56} fill="currentColor" opacity={b.k === 1 ? .10 : .04} />
              <line x1={l} x2={l} y1={60} y2={120} stroke="currentColor" strokeDasharray={b.dash} opacity=".6" />
              <line x1={r} x2={r} y1={60} y2={120} stroke="currentColor" strokeDasharray={b.dash} opacity=".6" />
              <text x={r + 3} y={58 - (b.k - 1) * 0} fontSize="10" fill="currentColor" opacity=".7">{b.label}{b.k > 1 ? " stress" : ""}</text>
            </g>
          );
        })}
        {hist != null && (
          <g>
            <line x1={x(S * (1 - hist))} x2={x(S * (1 - hist))} y1={70} y2={110} stroke="#8f5a12" strokeWidth="2" />
            <line x1={x(S * (1 + hist))} x2={x(S * (1 + hist))} y1={70} y2={110} stroke="#8f5a12" strokeWidth="2" />
            <text x={x(S * (1 + hist)) + 3} y={134} fontSize="10" fill="#8f5a12">hist. median ±{pct(String(hist))}</text>
          </g>
        )}
        <line x1={x(S)} x2={x(S)} y1={50} y2={125} stroke="currentColor" strokeWidth="2" />
        <text x={x(S)} y={44} fontSize="11" textAnchor="middle" fill="currentColor">spot {money(S, 2)}</text>
        {strikes.map((s, i) => {
          const hl = s.cid === highlightId;
          return (
            <g key={i}>
              <circle cx={x(s.strike)} cy={90} r={hl ? 6 : 3.5} fill={hl ? "#1f6273" : "currentColor"} opacity={hl ? 1 : .35} />
              {hl && <text x={x(s.strike)} y={108} fontSize="10" textAnchor="middle" fill="#1f6273">{s.action} {s.right[0]?.toUpperCase()} {fmtStrike(s.strike)}</text>}
            </g>
          );
        })}
        <text x={padL} y={H - 4} fontSize="10" fill="currentColor" opacity=".6">{money(lo, 0)}</text>
        <text x={W - padR} y={H - 4} fontSize="10" textAnchor="end" fill="currentColor" opacity=".6">{money(hi, 0)}</text>
      </svg>
      <p className="text-faint text-sm">
        Implied move from {em.implied_move_source ?? "—"}; ±1.5 and ±2 EM are deterministic stress points,
        not a probability distribution. Highlighted markers are the selected configuration's rank #1 legs.
      </p>
    </div>
  );
}

// --- T+1 scenario matrix (CORE and STRESS separate) ----------------------
function ScenarioMatrix({ cells, title, note }: { cells: V4ScenarioCell[]; title: string; note: string }) {
  if (!cells || cells.length === 0) {
    return <div className="card"><h2>{title}</h2><EmptyState>No frozen scenario cells for this candidate.</EmptyState></div>;
  }
  const moves = Array.from(new Map(cells.map((c) => [c.em_fraction, c.move_label])).entries())
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  const ivs = Array.from(new Map(cells.map((c) => [c.iv_multiplier, c.iv_label])).entries())
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  const at = (m: string, iv: string) => cells.find((c) => c.em_fraction === m && c.iv_multiplier === iv);
  const shade = (v: string | null) => {
    if (v == null) return "transparent";
    const n = Number(v);
    const a = Math.min(Math.abs(n) * 2.5, .5);
    return n >= 0 ? `rgba(43,107,71,${a})` : `rgba(149,50,46,${a})`;
  };
  return (
    <div className="card" data-testid={`scenario-matrix-${title.toLowerCase().includes("stress") ? "stress" : "core"}`}>
      <h2>{title}</h2>
      <div style={{ overflowX: "auto" }}>
        <table style={{ fontVariantNumeric: "tabular-nums" }}>
          <thead>
            <tr><th>Underlying move</th>{ivs.map(([k, l]) => <th key={k} style={{ textAlign: "right" }}>IV {l}</th>)}</tr>
          </thead>
          <tbody>
            {moves.map(([m, ml]) => (
              <tr key={m}>
                <td className="mono">{ml} <span className="text-faint">({Number(m) >= 0 ? "+" : ""}{Number(m).toFixed(1)} EM)</span></td>
                {ivs.map(([iv]) => {
                  const c = at(m, iv);
                  return (
                    <td key={iv} className="mono" style={{ textAlign: "right", background: shade(c?.return_executable ?? null) }}
                      title={c?.reason_codes?.join(", ")}>
                      {c ? pct(c.return_executable) : "—"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-faint text-sm">{note}</p>
    </div>
  );
}

// --- Candidate Explorer ----------------------------------------------------
function CandidateExplorer({ candidates, cfg, onHighlight }: {
  candidates: V4ShadowCandidate[]; cfg: V4ConfigResult | null; onHighlight: (id: string) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<"rank" | "core_median" | "core_worst" | "spread">("rank");
  const rankOf = (id: string) => {
    if (!cfg) return null;
    const i = cfg.ranked_candidate_ids.indexOf(id);
    return i >= 0 ? i + 1 : null;
  };
  const excluded = new Map((cfg?.exclusions ?? []).map((e) => [e.candidate_id, e]));
  const rows = useMemo(() => {
    const num = (v: string | null | undefined) => (v == null ? -Infinity : Number(v));
    const list = [...candidates];
    list.sort((a, b) => {
      if (sortKey === "rank") {
        const ra = rankOf(a.candidate_id) ?? 1e9, rb = rankOf(b.candidate_id) ?? 1e9;
        return ra - rb;
      }
      if (sortKey === "core_median") return num(b.core?.median_return) - num(a.core?.median_return);
      if (sortKey === "core_worst") return num(b.core?.worst_return) - num(a.core?.worst_return);
      return num(a.execution?.mean_relative_spread) - num(b.execution?.mean_relative_spread);
    });
    return list;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidates, cfg, sortKey]);

  if (candidates.length === 0) return <div className="card"><h2>Candidate Explorer</h2><EmptyState>No frozen candidates.</EmptyState></div>;
  const th = (label: string, key: typeof sortKey | null, right = true) => (
    <th style={{ textAlign: right ? "right" : "left", cursor: key ? "pointer" : undefined }} onClick={key ? () => setSortKey(key) : undefined}>
      {label}{key && sortKey === key ? " ▾" : ""}
    </th>
  );
  return (
    <div className="card" data-testid="candidate-explorer">
      <h2>Candidate Explorer <span className="text-faint text-sm">— {cfg ? cfg.label : "all"}</span></h2>
      <div style={{ overflowX: "auto" }}>
        <table style={{ fontVariantNumeric: "tabular-nums" }}>
          <thead>
            <tr>
              {th("Rank", "rank", false)}<th>Strategy</th><th>Expiration</th>
              {th("Capital", null)}{th("Max risk", null)}<th>Semantic fit</th>
              {th("Core worst", "core_worst")}{th("Core median", "core_median")}{th("Positive coverage", null)}
              {th("Stress worst", null)}{th("Spread", "spread")}<th>Validity</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => {
              const r = rankOf(c.candidate_id);
              const ex = excluded.get(c.candidate_id);
              const isOpen = open === c.candidate_id;
              return (
                <>
                  <tr key={c.candidate_id} onClick={() => { setOpen(isOpen ? null : c.candidate_id); onHighlight(c.candidate_id); }}
                    style={{ cursor: "pointer", opacity: ex ? .6 : 1 }}>
                    <td className="mono">{r ? `#${r}` : ex ? <span className="pill pill-warning" title={ex.detail}>{humanReasonCode(ex.reason_code)}</span> : "—"}</td>
                    <td>{humanStrategy(c.strategy)}</td>
                    <td className="mono">{c.expiration}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{money(c.capital?.entry_cash_required)}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{money(c.capital?.entry_cash_required)}</td>
                    <td>{c.semantic?.tier ?? "—"}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{pct(c.core?.worst_return)}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{pct(c.core?.median_return)}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{pct(c.core?.positive_scenario_fraction, 0)}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{pct(c.tail_stress?.worst_return)}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{pct(c.execution?.mean_relative_spread)}</td>
                    <td>{c.validity_status === "RANKABLE" ? <span className="pill pill-positive">Rankable</span> : <span className="pill pill-warning" title={c.status_reason ?? ""}>{c.validity_status.replace(/_/g, " ").toLowerCase()}</span>}</td>
                  </tr>
                  {isOpen && (
                    <tr key={`${c.candidate_id}-legs`}>
                      <td colSpan={12}>
                        <table className="legs-table" style={{ fontSize: ".82rem" }}>
                          <thead>
                            <tr><th>Leg</th><th>Action</th><th>Right</th><th style={{ textAlign: "right" }}>Strike</th><th>conId</th><th>Side</th>
                              <th style={{ textAlign: "right" }}>Side price</th><th style={{ textAlign: "right" }}>Bid</th><th style={{ textAlign: "right" }}>Ask</th>
                              <th style={{ textAlign: "right" }}>IV</th><th>Quality</th><th>Provider</th><th>Quoted</th></tr>
                          </thead>
                          <tbody>
                            {c.legs.map((l) => (
                              <tr key={l.leg_index}>
                                <td className="mono">{l.leg_index}</td><td>{l.action}</td><td>{l.right}</td>
                                <td className="mono" style={{ textAlign: "right" }}>{fmtStrike(l.strike)}</td>
                                <td className="mono">{l.external_contract_id ?? "—"}</td>
                                <td className="mono">{l.required_side?.toUpperCase() ?? "—"}</td>
                                <td className="mono" style={{ textAlign: "right" }}>{money(l.required_side_price, 2)}</td>
                                <td className="mono" style={{ textAlign: "right" }}>{money(l.bid, 2)}</td>
                                <td className="mono" style={{ textAlign: "right" }}>{money(l.ask, 2)}</td>
                                <td className="mono" style={{ textAlign: "right" }}>{fmtIv(l.implied_volatility)}</td>
                                <td><span className={`pill ${l.market_data_quality?.toLowerCase() === "delayed" ? "pill-warning" : "pill-neutral"}`}>{(l.market_data_quality ?? "—").toUpperCase()}</span></td>
                                <td className="mono">{l.source_provider ?? "—"}</td>
                                <td className="mono">{l.retrieved_at ? new Date(l.retrieved_at).toLocaleTimeString("en-US", { timeZone: "America/New_York" }) : "—"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        {c.data_quality_warnings?.warnings?.length ? (
                          <div className="warning text-sm">Warnings: {c.data_quality_warnings.warnings.join("; ")}</div>
                        ) : null}
                        {c.rank_explanation && <p className="text-muted text-sm">{c.rank_explanation}</p>}
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --- Forward outcome (Sections 23-26): entry observation / settlement --
export function ForwardOutcomePanel({ cfg, policy }: {
  cfg: V4ConfigResult | null; entry?: V4EntryObservation | null; settlement?: V4SettlementOutcome | null; policy?: string;
}) {
  if (!cfg) return null;
  const life = cfg.lifecycle ?? cfg.status;
  const e = cfg.entry ?? null;
  const st = cfg.settlement ?? null;
  const legs = (e?.legs ?? st?.legs ?? null);
  return (
    <div className="card" data-testid="forward-outcome">
      <SectionHeader title="Forward outcome" eyebrow={cfg.label} right={<LifecyclePill lifecycle={life} />} />
      {life === "NO_ACTION" && (
        <p className="text-muted" style={{ margin: 0 }}>No candidate satisfied the methodology for this configuration. This is a recorded outcome, not a failure. {cfg.no_action_reason ? <span className="text-faint">({cfg.no_action_reason})</span> : null}</p>
      )}
      {life === "FAILED" && <p className="negative" style={{ margin: 0 }}>{cfg.no_action_reason}</p>}
      {life === "WAITING_ENTRY" && (
        <p className="text-muted" style={{ margin: 0 }}>Waiting for this configuration's own entry observation at the 15:30 ET decision window.</p>
      )}
      {life === "ENTRY_FAILED" && e && (
        <FailureExplanation category={e.failure_category} detail={e.failure_detail} provider="ibkr_tws" quality={e.market_data_quality} retryable={false}
          requiredSide={legs?.find((l) => l.price == null)?.required_side ?? null} />
      )}
      {(life === "WAITING_SETTLEMENT" || life === "ENTRY_OBSERVED") && e && (
        <>
          <div className="grid grid-4" style={{ gap: 8 }}>
            <Metric label="Position" value={`${e.quantity} × ${humanStrategy(cfg.rank_1?.strategy)}`} sub={<span className="mono">{e.candidate_id}</span>} />
            <Metric label="Capital used" value={money(e.capital_used, 2)} mono sub={`max risk ${money(e.max_risk_used, 2)} of ${money(cfg.max_risk_dollars)}`} />
            <Metric label="Entry value" value={money(e.entry_net_value, 2)} mono sub={<Timestamp iso={e.observed_at} />} />
            <Metric label="Market data" value={<MarketDataQualityBadge quality={e.market_data_quality} provider="ibkr_tws" />} sub={e.pricing_convention.replace(/_/g, " ").toLowerCase()} />
          </div>
          <p className="text-faint text-sm" style={{ margin: "8px 0 0" }}>Waiting for post-earnings settlement observation ({policy ?? "T+1 at 15:55 ET"}). No interim P&amp;L is shown.</p>
        </>
      )}
      {life === "SETTLED" && st && (
        <>
          <div className="grid grid-4" style={{ gap: 8 }}>
            <Metric label="Position" value={`${st.quantity} × ${humanStrategy(cfg.rank_1?.strategy)}`} sub={<span className="mono">{st.candidate_id}</span>} />
            <Metric label="Entry → exit" value={`${money(st.entry_net_value, 2)} → ${money(st.exit_net_value, 2)}`} mono sub={<><Timestamp iso={st.entry_observed_at} /> → <Timestamp iso={st.settled_at} /></>} />
            <Metric label="Realized P&L" value={<span className={Number(st.realized_pnl ?? 0) >= 0 ? "positive" : "negative"}>{money(st.realized_pnl, 2)}</span>} mono sub={`capital used ${money(st.capital_used, 2)}`} />
            <Metric label="Standardized return" value={pct(st.return_on_standardized_capital, 2)} mono sub={<MarketDataQualityBadge quality={st.market_data_quality} provider="ibkr_tws" />} />
          </div>
          <p className="text-faint text-sm" style={{ margin: "8px 0 0" }}>{st.pricing_convention.replace(/_/g, " ").toLowerCase()} · One observation. No statistical significance is implied.</p>
        </>
      )}
      {life === "SETTLEMENT_FAILED" && st && (
        <FailureExplanation category={st.failure_category} detail={st.failure_detail} provider="ibkr_tws" quality={st.market_data_quality} retryable={false} />
      )}
      {legs && legs.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="text-muted text-sm">Observed legs — required sides and quotes</summary>
          <table style={{ marginTop: 6, fontSize: ".8rem" }}>
            <thead><tr><th>Leg</th><th>Side</th><th>conId</th><th style={{ textAlign: "right" }}>Required</th><th style={{ textAlign: "right" }}>Price</th><th style={{ textAlign: "right" }}>Bid</th><th style={{ textAlign: "right" }}>Ask</th><th>Quality</th></tr></thead>
            <tbody>
              {legs.map((l) => (
                <tr key={l.leg_index}>
                  <td className="mono">{l.action} {l.right[0]?.toUpperCase()} {fmtStrike(l.strike)}</td>
                  <td className="mono">{l.required_side.toUpperCase()}</td>
                  <td className="mono">{l.external_contract_id ?? "—"}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{l.required_side.toUpperCase()}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{money(l.price, 2)}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{money(l.bid, 2)}</td>
                  <td className="mono" style={{ textAlign: "right" }}>{money(l.ask, 2)}</td>
                  <td><span className={`pill ${(l.market_data_quality ?? "").toLowerCase() === "delayed" ? "pill-warning" : "pill-neutral"}`}>{(l.market_data_quality ?? "—").toUpperCase()}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

// --- Page ------------------------------------------------------------------
// Embeddable decision view (used by the page below AND by the company
// workspace, Section 6 -- one implementation, never duplicated).
export function V4DecisionView({ decisionId, mode = "lab", backTo, hideHero = false }: {
  decisionId: number; mode?: "lab" | "explorer"; backTo?: string; hideHero?: boolean;
}) {
  const [selected, setSelected] = useState<string>("v4_2k_moderate");
  const [highlight, setHighlight] = useState<string | null>(null);
  const cfgs = useAsync(() => api.getV4ShadowConfigurations(decisionId), [decisionId]);
  const cands = useAsync(() => api.getV4ShadowCandidates(decisionId), [decisionId]);

  if ((cfgs.loading && !cfgs.data) || (cands.loading && !cands.data)) return <LoadingState label="Loading frozen V4 evidence…" />;
  if (cfgs.error && !cfgs.data) return <ErrorState message={cfgs.error} />;
  const data = cfgs.data as V4ShadowConfigurationsResponse | null;
  if (!data) return <ErrorState message="Decision not found." />;
  const cfg = data.configurations.find((c) => c.configuration_key === selected) ?? null;
  const candidates = cands.data?.candidates ?? [];
  const topId = cfg?.rank_1_candidate_id ?? null;
  const topCandidate = candidates.find((c) => c.candidate_id === (highlight ?? topId)) ?? null;
  const grid = topCandidate?.scenario_grid ?? null;

  return (
    <div>
      {backTo && (
        <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
          <Link className="btn-secondary" to={backTo}>← All decisions</Link>
          <Link className="btn-secondary" to={`/same-event-comparison/${data.decision.earnings_calendar_event_id}`}>Same-event comparison →</Link>
        </div>
      )}
      <ExperimentalNotice text={data.notice} />
      {mode === "lab" && !hideHero && <Hero d={data.decision} cfg={cfg} timing={data.timing_policy_version} />}
      <ConfigSelector configs={data.configurations} selected={selected} onSelect={(k) => { setSelected(k); setHighlight(null); }} />
      {mode === "lab" && <ForwardOutcomePanel cfg={cfg} entry={data.entry_observation} settlement={data.settlement} policy={data.settlement_policy} />}
      {mode === "lab" && <ConfigComparison configs={data.configurations} selected={selected} onSelect={(k) => { setSelected(k); setHighlight(null); }} />}
      {mode === "lab" && cfg && <WhyThisStrategy cfg={cfg} candidates={data.candidates} />}
      {mode === "lab" && <ExpectedMoveChart em={data.decision.expected_move} spot={data.decision.market_data?.underlying_price} candidates={candidates} highlightId={highlight ?? topId} />}
      {mode === "lab" && topCandidate && (
        <>
          <ScenarioMatrix cells={grid?.core ?? []} title={`T+1 modeled executable return — core (${humanStrategy(topCandidate.strategy)})`}
            note="Modeled executable T+1 return on standardized capital, per underlying-move × IV scenario. A scenario average is not an expected return; coverage is not a probability." />
          <ScenarioMatrix cells={grid?.stress ?? []} title="T+1 — tail stress (±1.5 / ±2 EM)"
            note="Deterministic stress points only. Never averaged into the core grid; not a probability." />
        </>
      )}
      <CandidateExplorer candidates={candidates} cfg={cfg} onHighlight={setHighlight} />
    </div>
  );
}

// --- Page ------------------------------------------------------------------
export function V4DecisionLab({ mode = "lab" }: { mode?: "lab" | "explorer" }) {
  const params = useParams<{ id?: string }>();
  const navigate = useNavigate();
  const id = params.id ? Number(params.id) : null;
  const base = mode === "explorer" ? "/candidate-explorer" : "/v4-decision-lab";
  return (
    <div>
      <div className="page-header"><h1>{mode === "explorer" ? "Candidate Explorer" : "V4 Decision Lab"}</h1></div>
      {!id ? (
        <>
          <ExperimentalNotice />
          <DecisionPicker mode={mode} onPick={(d) => navigate(`${base}/${d}`)} />
        </>
      ) : (
        <V4DecisionView decisionId={id} mode={mode} backTo={base} />
      )}
    </div>
  );
}
