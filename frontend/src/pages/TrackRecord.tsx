import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { HistoricalCompatibilityValue } from "../components/HistoricalCompatibility";
import {
  deriveLifecycleStage,
  LIFECYCLE_LABELS,
  LIFECYCLE_PILL_CLASS,
  TIMING_LABELS,
} from "../lib/decisionLifecycle";
import { HISTORICAL_MOVE_COMPATIBILITY_EXPLANATION } from "../lib/historicalCompatibility";
import type {
  DecisionDirection,
  DecisionSnapshot,
  EarningsCalendarEvent,
  EntryCaptureAttempt,
  Rate,
  SettlementCaptureAttempt,
} from "../types/api";

const DIRECTION_LABELS: Record<DecisionDirection, string> = {
  strong_bullish: "Strongly Bullish",
  bullish: "Bullish",
  neutral: "Neutral",
  bearish: "Bearish",
  strong_bearish: "Strongly Bearish",
};

function pct(rate: Rate): string {
  if (rate.pct === null) return "—";
  return `${(Number(rate.pct) * 100).toFixed(0)}%`;
}

function PendingFinalDecisions() {
  const pendingState = useAsync(() => api.getPendingDecisions(), []);
  const [settlingId, setSettlingId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Record<number, string>>({});

  const trySettle = async (ticker: string, id: number) => {
    setSettlingId(id);
    setMessages((prev) => ({ ...prev, [id]: "" }));
    try {
      const result = await api.settleDecision(ticker, id);
      setMessages((prev) => ({ ...prev, [id]: result.message }));
      pendingState.reload();
    } catch (err) {
      setMessages((prev) => ({
        ...prev,
        [id]: err instanceof ApiError ? err.message : "Could not attempt settlement.",
      }));
    } finally {
      setSettlingId(null);
    }
  };

  if (pendingState.loading && !pendingState.data) return null;
  if (pendingState.error && !pendingState.data) return null;
  if (!pendingState.data || pendingState.data.final_count === 0) return null;

  const { pending, final_count, pending_count, settled_count } = pendingState.data;

  return (
    <div className="card">
      <h2>Pending Final Decisions</h2>
      <p className="text-sm text-muted" style={{ marginTop: 0 }}>
        {final_count} Final Decision{final_count === 1 ? "" : "s"} marked overall — {settled_count}{" "}
        settled, {pending_count} still awaiting a real post-earnings outcome.
      </p>
      {pending.length === 0 ? (
        <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
          Every Final Decision has been settled.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Direction</th>
              <th>Generated</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {pending.map(({ ticker, decision }) => (
              <tr key={decision.id}>
                <td className="mono">{ticker}</td>
                <td>{DIRECTION_LABELS[decision.direction]}</td>
                <td className="text-sm text-faint">
                  {new Date(decision.created_at).toLocaleDateString()}
                </td>
                <td className="text-sm text-muted">{decision.settlement_reason}</td>
                <td>
                  <button
                    className="btn-secondary"
                    onClick={() => trySettle(ticker, decision.id)}
                    disabled={settlingId === decision.id}
                  >
                    {settlingId === decision.id ? "Attempting…" : "Attempt Settlement"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {Object.entries(messages)
        .filter(([, message]) => message)
        .map(([id, message]) => (
          <div key={id} className="notice" style={{ marginTop: 8, marginBottom: 0 }}>
            {message}
          </div>
        ))}
    </div>
  );
}

function RateRow({ label, rate }: { label: string; rate: Rate }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{pct(rate)}</span>
      <span className="text-sm text-faint">
        {rate.total === 0 ? "N = 0" : `${rate.correct} of ${rate.total} final decisions (N = ${rate.total})`}
      </span>
    </div>
  );
}

// --------------------------------------------------------------------------
// Post-live correction (2026-08-25) -- Section 4: this page was entirely
// backed by the legacy AIDecisionVersion journal (services/track_
// record.py, GET /research/track-record) and had no idea the real,
// official forward-test DecisionSnapshot pipeline (Phase 4) even
// existed -- Aug 25's 8 real DecisionSnapshots were invisible here. This
// section reads that pipeline's own already-existing, read-only
// endpoints (GET /decision-snapshots, GET /benchmark/entries, GET
// /settlements) -- the same ones the Dashboard and Operations already
// use -- never a new write path, never merged with the legacy journal
// below it. No fabricated performance metric: this is a real, honest
// archive of what was decided/entered/settled, not a win-rate claim --
// see BenchmarkTrackRecord.tsx for real performance metrics, which
// still require real settlement.
// --------------------------------------------------------------------------

async function fetchOfficialArchive() {
  const [decisions, entries, settlements, events] = await Promise.all([
    api.listDecisionSnapshots({ limit: 200 }),
    api.listBenchmarkEntries({ limit: 200 }),
    api.listAllSettlements({ status: "captured", limit: 200 }),
    // Best-effort: only covers events still status=UPCOMING (no bulk
    // "by id, any status" endpoint exists) -- a genuinely archived event
    // falls back to "—" below rather than failing the whole page.
    api.listUpcomingEarnings({ limit: 200 }),
  ]);
  return { decisions, entries, settlements, events };
}

function OfficialDecisionArchive() {
  const archive = useAsync(fetchOfficialArchive, []);

  if (archive.loading && !archive.data) return <LoadingState label="Loading official archive…" />;
  if (archive.error && !archive.data) return <ErrorState message={archive.error} />;
  if (!archive.data) return null;

  const { decisions, entries, settlements, events } = archive.data;

  const eventById = new Map(events.map((e) => [e.id, e]));
  const entriesByDecisionId = new Map<number, EntryCaptureAttempt[]>();
  for (const e of entries) {
    const list = entriesByDecisionId.get(e.decision_snapshot_id) ?? [];
    list.push(e);
    entriesByDecisionId.set(e.decision_snapshot_id, list);
  }
  const settlementsByDecisionId = new Map<number, SettlementCaptureAttempt[]>();
  for (const s of settlements) {
    const list = settlementsByDecisionId.get(s.decision_snapshot_id) ?? [];
    list.push(s);
    settlementsByDecisionId.set(s.decision_snapshot_id, list);
  }

  const sorted = [...decisions].sort((a, b) => b.generated_at.localeCompare(a.generated_at));

  return (
    <div className="card">
      <h2>Official Forward-Test Decision Archive</h2>
      <p className="text-sm text-muted" style={{ marginTop: 0 }}>
        Every real, immutable DecisionSnapshot frozen by the official scheduled pipeline —
        distinct from the on-demand journal below. No settlement, no outcome metric: real
        performance requires a real, captured post-earnings exit (see Benchmark Track Record).
      </p>
      {sorted.length === 0 ? (
        <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
          No official decisions have been generated yet.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Earnings</th>
              <th>Generated</th>
              <th>Direction</th>
              <th>Strategy</th>
              <th>Expiration</th>
              <th title={HISTORICAL_MOVE_COMPATIBILITY_EXPLANATION}>Hist. Compatibility</th>
              <th>Score</th>
              <th>Entry</th>
              <th>Settlement</th>
              <th>Data Quality</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((d) => (
              <ArchiveRow
                key={d.id}
                decision={d}
                entries={entriesByDecisionId.get(d.id) ?? []}
                settlements={settlementsByDecisionId.get(d.id) ?? []}
                event={eventById.get(d.earnings_calendar_event_id)}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Phase 4 market-data-quality hardening (2026-08-26), Section 17 -- a
// real, honest label per row, never invisibly combining a delayed
// capture with a live one. Prefers the most recent settlement's label
// (the fuller, later picture) when one exists, else the entry's.
// --------------------------------------------------------------------------

const QUALITY_PILL_CLASS: Record<string, string> = {
  VERIFIED_LIVE: "pill-positive",
  DELAYED_DATA: "pill-warning",
  UNKNOWN_QUALITY: "pill-neutral",
};

const QUALITY_LABEL_TEXT: Record<string, string> = {
  VERIFIED_LIVE: "Verified Live",
  DELAYED_DATA: "Delayed Data",
  UNKNOWN_QUALITY: "Unknown Quality",
};

function DataQualityPill({
  entries,
  settlements,
}: {
  entries: EntryCaptureAttempt[];
  settlements: SettlementCaptureAttempt[];
}) {
  const source = settlements.length > 0 ? settlements[settlements.length - 1] : entries[entries.length - 1];
  if (!source) return <span className="text-faint text-sm">—</span>;
  const label = source.market_data_quality_label;
  return (
    <span
      className={`pill ${QUALITY_PILL_CLASS[label] ?? "pill-neutral"}`}
      title="Real, per-capture market-data quality — never invisibly combined across a live and a delayed capture."
    >
      {QUALITY_LABEL_TEXT[label] ?? label}
    </span>
  );
}

// --------------------------------------------------------------------------
// Phase 4 quote-observability hardening (2026-08-26), Section 13 --
// expandable quote-acquisition diagnostics attached to the row's own
// entry/settlement attempt, fetched only once expanded (never on every
// row's mount). Mirrors Operations.tsx's PipelineRow expand pattern.
// --------------------------------------------------------------------------

function ArchiveRow({
  decision,
  entries,
  settlements,
  event,
}: {
  decision: DecisionSnapshot;
  entries: EntryCaptureAttempt[];
  settlements: SettlementCaptureAttempt[];
  event: EarningsCalendarEvent | undefined;
}) {
  const [expanded, setExpanded] = useState(false);
  const stage = deriveLifecycleStage(entries, settlements, decision);
  const failedEntry = [...entries].reverse().find((e) => e.status === "failed");
  const failedSettlement = [...settlements].reverse().find((s) => s.status === "failed");
  const diagnosticAttempt = failedEntry ?? failedSettlement;
  const clickable = diagnosticAttempt !== undefined;

  return (
    <>
      <tr
        onClick={clickable ? () => setExpanded((e) => !e) : undefined}
        style={clickable ? { cursor: "pointer" } : undefined}
      >
        <td className="mono">
          <Link
            to={`/company/${decision.ticker}`}
            className="text-link"
            onClick={(e) => e.stopPropagation()}
          >
            {decision.ticker}
          </Link>
        </td>
        <td className="text-sm mono">
          {event
            ? `${new Date(`${event.earnings_date}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${TIMING_LABELS[event.earnings_time]}`
            : "—"}
        </td>
        <td className="text-sm mono">
          {new Date(decision.generated_at).toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
          })}
        </td>
        <td className="mono">{decision.strategy_direction}</td>
        <td className="mono">{decision.strategy_type ?? "No action"}</td>
        <td className="mono">{decision.selected_expiration ?? "—"}</td>
        <td className="mono">
          <HistoricalCompatibilityValue snapshot={decision} compact />
        </td>
        <td className="mono">{decision.strategy_score !== null ? decision.strategy_score : "—"}</td>
        <td>
          <span className={`pill ${LIFECYCLE_PILL_CLASS[stage]}`}>{LIFECYCLE_LABELS[stage]}</span>
          {clickable && (
            <span className="text-faint text-sm" style={{ marginLeft: 6 }}>
              {expanded ? "▾" : "▸"} diagnostics
            </span>
          )}
        </td>
        <td className="text-sm">
          {settlements.some((s) => s.status === "captured") ? "Settled" : "—"}
        </td>
        <td>
          <DataQualityPill entries={entries} settlements={settlements} />
        </td>
      </tr>
      {expanded && diagnosticAttempt && (
        <tr>
          <td colSpan={11} style={{ background: "var(--color-bg)" }}>
            <QuoteDiagnosticsPanel
              ticker={decision.ticker}
              entryCaptureAttemptId={failedEntry?.id}
              settlementCaptureAttemptId={failedEntry ? undefined : failedSettlement?.id}
            />
          </td>
        </tr>
      )}
    </>
  );
}

function QuoteDiagnosticsPanel({
  ticker,
  entryCaptureAttemptId,
  settlementCaptureAttemptId,
}: {
  ticker: string;
  entryCaptureAttemptId: number | undefined;
  settlementCaptureAttemptId: number | undefined;
}) {
  const diagnostics = useAsync(() => {
    if (entryCaptureAttemptId !== undefined) {
      return api.getEntryQuoteDiagnostics(entryCaptureAttemptId);
    }
    if (settlementCaptureAttemptId !== undefined) {
      return api.getSettlementQuoteDiagnostics(settlementCaptureAttemptId);
    }
    return Promise.reject(new Error("no capture attempt id"));
  }, [entryCaptureAttemptId, settlementCaptureAttemptId]);

  if (diagnostics.loading && !diagnostics.data) {
    return (
      <div className="text-sm text-muted" style={{ padding: "8px 4px" }}>
        Loading quote diagnostics…
      </div>
    );
  }
  if (diagnostics.error || !diagnostics.data) {
    return (
      <div className="text-sm text-muted" style={{ padding: "8px 4px" }}>
        No quote-acquisition telemetry on record for this attempt — likely a legacy capture
        predating this diagnostic table.
      </div>
    );
  }

  return (
    <div style={{ padding: "8px 4px" }}>
      <div className="text-sm text-muted mono" style={{ marginBottom: 6 }}>
        {ticker} · ENTRY QUOTE DIAGNOSTICS
      </div>
      {diagnostics.data.legs.map((leg, i) => (
        <div key={i} style={{ marginBottom: 10 }}>
          <div className="text-sm mono" style={{ marginBottom: 2 }}>
            Leg {leg.leg_index ?? "—"} · {leg.option_type?.toUpperCase() ?? "—"}{" "}
            {leg.strike ?? "—"}
          </div>
          <div className="text-sm text-muted" style={{ marginBottom: 4 }}>
            Required executable side: {leg.required_side.toUpperCase()} · Contract resolved:{" "}
            {leg.contract_resolved ? "yes" : "no"}
          </div>
          {leg.attempts.map((a) => (
            <div
              key={a.snapshot_attempt_number}
              className="text-sm mono text-faint"
              style={{ paddingLeft: 12 }}
            >
              Attempt {a.snapshot_attempt_number} — elapsed: {a.elapsed_ms}ms · bid:{" "}
              {a.bid ?? "—"} · ask: {a.ask ?? "—"} · last: {a.last_price ?? "—"}
              {a.market_data_quality ? ` · quality: ${a.market_data_quality}` : ""}
            </div>
          ))}
          <div className="text-sm" style={{ marginTop: 4, fontWeight: 600 }}>
            Result: {leg.result_label}
          </div>
        </div>
      ))}
    </div>
  );
}

export function TrackRecord() {
  const [window, setWindow] = useState<"all_time" | "last_10">("all_time");
  const [ticker, setTicker] = useState("");
  const record = useAsync(
    () => api.getTrackRecord({ window, ticker: ticker.trim() || undefined }),
    [window, ticker]
  );

  if (record.loading && !record.data) return <LoadingState label="Loading track record…" />;
  if (record.error && !record.data) return <ErrorState message={record.error} />;
  if (!record.data) return null;

  const r = record.data;

  return (
    <div>
      <div className="page-header">
        <h1>AI Decision Track Record</h1>
        <p>
          Honest reliability metrics for the AI Options Decision Engine — computed only over
          decisions with real, settled post-earnings outcomes. Historical accuracy does not imply
          future profitability. Some older decisions lack real point-in-time options pricing, so
          strategy-level P&amp;L statistics are only ever shown when valid option snapshots were
          actually captured — never estimated.
        </p>
      </div>

      <OfficialDecisionArchive />

      <div className="page-header" style={{ marginTop: 24 }}>
        <h1 style={{ fontSize: 18 }}>On-Demand / Legacy Analysis</h1>
        <p>
          The original AI Options Decision journal — decisions you generate and finalize manually
          from the Search page, graded against real settled outcomes below. A separate system from
          the official archive above; nothing here is ever merged into or overwritten by it.
        </p>
      </div>

      <PendingFinalDecisions />

      <div className="card" style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 4 }}>
          <button
            className={`tab-button ${window === "all_time" ? "active" : ""}`}
            onClick={() => setWindow("all_time")}
          >
            All Time
          </button>
          <button
            className={`tab-button ${window === "last_10" ? "active" : ""}`}
            onClick={() => setWindow("last_10")}
          >
            Last 10 Decisions
          </button>
        </div>
        <input
          type="text"
          placeholder="Filter by ticker (optional)"
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          style={{ maxWidth: 220 }}
        />
      </div>

      {r.evaluated_count === 0 ? (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            No settled decisions yet{ticker ? ` for ${ticker}` : ""}. Metrics appear once real
            post-earnings price data exists for at least one generated decision.
          </p>
        </div>
      ) : (
        <>
          <div className="grid grid-3" style={{ gap: 16 }}>
            <div className="card">
              <RateRow label="Directional Accuracy" rate={r.directional_accuracy} />
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Did the stock move in the predicted direction.
              </p>
            </div>
            <div className="card">
              <RateRow label="Bullish-Decision Accuracy" rate={r.bullish_accuracy} />
            </div>
            <div className="card">
              <RateRow label="Bearish-Decision Accuracy" rate={r.bearish_accuracy} />
            </div>
            <div className="card">
              <RateRow label="Breakeven Success" rate={r.breakeven_success} />
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Did the underlying finish beyond the recommended strategy's breakeven.
              </p>
            </div>
            <div className="card">
              <RateRow label="Implied-Move-View Accuracy" rate={r.volatility_view_accuracy} />
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Long-vol calls correct when the actual move exceeded the implied move; short-vol
                calls correct when it did not.
              </p>
            </div>
            <div className="card">
              <RateRow label="High-Confidence Accuracy (≥70)" rate={r.high_confidence_accuracy} />
            </div>
          </div>

          <div className="card">
            <h2>Strategy Win Rate</h2>
            <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
              {r.strategy_win_rate_available
                ? "Available for decisions with real point-in-time option entry/exit prices."
                : "Not available — this project has not yet captured real point-in-time option " +
                  "entry/exit prices for any settled decision. Directional Accuracy and Breakeven " +
                  "Success above are real, distinct metrics and must not be read as a win rate."}
            </p>
          </div>

          <div className="card">
            <h2>Average Confidence</h2>
            <span className="stat-value">
              {r.average_confidence ? Number(r.average_confidence).toFixed(0) : "—"} / 100
            </span>
          </div>

          {r.confidence_calibration.some((b) => b.rate.total > 0) && (
            <div className="card">
              <h2>Confidence Calibration</h2>
              <table>
                <thead>
                  <tr>
                    <th>Confidence range</th>
                    <th>N</th>
                    <th>Realized directional accuracy</th>
                  </tr>
                </thead>
                <tbody>
                  {r.confidence_calibration.map((b) => (
                    <tr key={b.label}>
                      <td className="mono">{b.label}</td>
                      <td className="mono">{b.rate.total}</td>
                      <td className="mono">{b.rate.total > 0 ? pct(b.rate) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
                Whether higher stated confidence actually correlates with higher realized accuracy
                — buckets with very few decisions are not statistically meaningful yet.
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
