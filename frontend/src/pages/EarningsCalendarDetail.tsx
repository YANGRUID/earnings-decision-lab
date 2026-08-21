import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import {
  deriveLifecycleStage,
  earningsCountdownLabel,
  LIFECYCLE_LABELS,
  TIMING_LABELS,
} from "../lib/decisionLifecycle";
import { formatMoney, formatPlainPercent } from "../lib/format";
import type {
  AIThesisVersion,
  DecisionSnapshot,
  EarningsCalendarEvent,
  EntryCaptureAttempt,
  SettlementCaptureAttempt,
} from "../types/api";

interface DetailData {
  event: EarningsCalendarEvent | null;
  decision: DecisionSnapshot | null;
  entries: EntryCaptureAttempt[];
  settlements: SettlementCaptureAttempt[];
  thesis: AIThesisVersion | null;
}

/** Every calendar entry on record for this symbol, upcoming and past
 * alike (GET /earnings-calendar/{symbol} already returns both) -- picks
 * the nearest upcoming one, or the most recent past one if none is
 * upcoming, as this page's "primary" event. Deliberately client-side:
 * the backend endpoint intentionally returns the full real history
 * rather than pre-selecting one (see api/routers/earnings_calendar.py). */
function pickPrimaryEvent(events: EarningsCalendarEvent[]): EarningsCalendarEvent | null {
  if (events.length === 0) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const upcoming = events
    .filter((e) => new Date(`${e.earnings_date}T00:00:00`) >= today)
    .sort((a, b) => a.earnings_date.localeCompare(b.earnings_date));
  if (upcoming.length > 0) return upcoming[0];
  return [...events].sort((a, b) => b.earnings_date.localeCompare(a.earnings_date))[0];
}

async function fetchDetail(symbol: string): Promise<DetailData> {
  const events = await api.getSymbolEarningsCalendar(symbol);
  const event = pickPrimaryEvent(events);

  const decisions = await api.listDecisionSnapshots({ ticker: symbol, limit: 50 });
  const decision =
    (event ? decisions.find((d) => d.earnings_calendar_event_id === event.id) : undefined) ??
    null;

  let entries: EntryCaptureAttempt[] = [];
  let settlements: SettlementCaptureAttempt[] = [];
  let thesis: AIThesisVersion | null = null;
  if (decision) {
    [entries, settlements] = await Promise.all([
      api.getDecisionSnapshotEntries(decision.id),
      api.getSettlements(decision.id),
    ]);
    if (decision.ai_thesis_version_id !== null) {
      try {
        thesis = await api.getThesisVersion(symbol, decision.ai_thesis_version_id);
      } catch {
        thesis = null; // thesis may have been superseded/deleted -- an honest gap, not an error
      }
    }
  }

  return { event, decision, entries, settlements, thesis };
}

function LegsTable({
  legs,
}: {
  legs: { action: string; option_type: string; strike: string; quantity: number; premium?: string }[];
}) {
  return (
    <table className="legs-table">
      <thead>
        <tr>
          <th>Action</th>
          <th>Qty</th>
          <th>Type</th>
          <th>Strike</th>
          {legs[0]?.premium !== undefined && <th>Premium</th>}
        </tr>
      </thead>
      <tbody>
        {legs.map((leg, i) => (
          <tr key={i}>
            <td className="mono">{leg.action}</td>
            <td className="mono">{leg.quantity}</td>
            <td className="mono">{leg.option_type}</td>
            <td className="mono">{formatMoney(leg.strike)}</td>
            {leg.premium !== undefined && <td className="mono">{formatMoney(leg.premium)}</td>}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ThesisCard({ thesis }: { thesis: AIThesisVersion }) {
  const sections: { label: string; text: string }[] = [
    { label: "Market Setup", text: thesis.market_setup },
    { label: "Business Context", text: thesis.business_context },
    { label: "Historical Earnings Pattern", text: thesis.historical_earnings_pattern },
    { label: "Guidance Trend", text: thesis.guidance_trend },
    { label: "Key Risks", text: thesis.key_risks },
  ];
  return (
    <div className="card">
      <h2>AI Thesis</h2>
      {sections.map((s) => (
        <div key={s.label} style={{ marginBottom: 12 }}>
          <div style={{ fontWeight: 600, fontSize: 13 }}>{s.label}</div>
          <p className="text-sm text-muted" style={{ margin: "4px 0 0" }}>
            {s.text}
          </p>
        </div>
      ))}
      <p className="text-sm text-faint" style={{ marginBottom: 0 }}>
        {thesis.disclaimer}
      </p>
    </div>
  );
}

function EntryStatusCard({ entries }: { entries: EntryCaptureAttempt[] }) {
  const captured = entries.find((e) => e.status === "captured");
  const latestFailed = [...entries].reverse().find((e) => e.status === "failed");

  return (
    <div className="card">
      <h2>Entry Status</h2>
      {captured ? (
        <>
          <div className="grid grid-3" style={{ gap: 10 }}>
            <div className="stat">
              <span className="stat-label">Underlying at entry</span>
              <span className="stat-value small">
                {captured.underlying_price ? `$${formatMoney(captured.underlying_price)}` : "—"}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">Net entry cash</span>
              <span className="stat-value small">
                {captured.net_entry_cash ? `$${formatMoney(captured.net_entry_cash)}` : "—"}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">Max risk</span>
              <span className="stat-value small">
                {captured.initial_max_risk ? `$${formatMoney(captured.initial_max_risk)}` : "—"}
              </span>
            </div>
          </div>
          <table className="legs-table" style={{ marginTop: 10 }}>
            <thead>
              <tr>
                <th>Action</th>
                <th>Strike</th>
                <th>Type</th>
                <th>Bid</th>
                <th>Ask</th>
                <th>Fill</th>
              </tr>
            </thead>
            <tbody>
              {captured.legs.map((leg) => (
                <tr key={leg.id}>
                  <td className="mono">{leg.action}</td>
                  <td className="mono">{formatMoney(leg.strike)}</td>
                  <td className="mono">{leg.option_type}</td>
                  <td className="mono">{leg.bid ? formatMoney(leg.bid) : "—"}</td>
                  <td className="mono">{leg.ask ? formatMoney(leg.ask) : "—"}</td>
                  <td className="mono">
                    {leg.benchmark_entry_price ? formatMoney(leg.benchmark_entry_price) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          {latestFailed
            ? `Entry not yet captured — last attempt failed: ${latestFailed.capture_error}`
            : "Pending entry — no capture attempt yet."}
        </p>
      )}
    </div>
  );
}

function SettlementStatusCard({ settlements }: { settlements: SettlementCaptureAttempt[] }) {
  const captured = settlements.find((s) => s.status === "captured");
  const latestFailed = [...settlements].reverse().find((s) => s.status === "failed");

  return (
    <div className="card">
      <h2>Settlement Status</h2>
      {captured ? (
        <>
          <div className="grid grid-3" style={{ gap: 10 }}>
            <div className="stat">
              <span className="stat-label">Realized P&amp;L</span>
              <span className={`stat-value small ${Number(captured.realized_pnl) >= 0 ? "positive" : "negative"}`}>
                {captured.realized_pnl ? `$${formatMoney(captured.realized_pnl)}` : "—"}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">Return %</span>
              <span className="stat-value small">
                {captured.return_pct ? `${formatMoney(captured.return_pct, 1)}%` : "—"}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">R Multiple</span>
              <span className="stat-value small">
                {captured.r_multiple ? Number(captured.r_multiple).toFixed(2) : "—"}
              </span>
            </div>
          </div>
          <table className="legs-table" style={{ marginTop: 10 }}>
            <thead>
              <tr>
                <th>Action</th>
                <th>Strike</th>
                <th>Type</th>
                <th>Bid</th>
                <th>Ask</th>
                <th>Fill</th>
              </tr>
            </thead>
            <tbody>
              {captured.legs.map((leg) => (
                <tr key={leg.id}>
                  <td className="mono">{leg.action}</td>
                  <td className="mono">{formatMoney(leg.strike)}</td>
                  <td className="mono">{leg.option_type}</td>
                  <td className="mono">{leg.bid ? formatMoney(leg.bid) : "—"}</td>
                  <td className="mono">{leg.ask ? formatMoney(leg.ask) : "—"}</td>
                  <td className="mono">
                    {leg.benchmark_exit_price ? formatMoney(leg.benchmark_exit_price) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p className="text-sm text-muted" style={{ margin: 0 }}>
          {latestFailed
            ? `No settled trades available — last exit attempt failed: ${latestFailed.capture_error}`
            : "No settled trades available."}
        </p>
      )}
    </div>
  );
}

export function EarningsCalendarDetail() {
  const { symbol = "" } = useParams();
  const detail = useAsync(() => fetchDetail(symbol.toUpperCase()), [symbol]);

  if (detail.loading && !detail.data) return <LoadingState label="Loading earnings detail…" />;
  if (detail.error && !detail.data) return <ErrorState message={detail.error} />;
  if (!detail.data) return null;

  const { event, decision, entries, settlements, thesis } = detail.data;

  if (!event) {
    return (
      <div>
        <div className="page-header">
          <h1>{symbol.toUpperCase()}</h1>
        </div>
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            No earnings calendar entry on record for this symbol yet.
          </p>
        </div>
      </div>
    );
  }

  const stage = decision ? deriveLifecycleStage(entries, settlements) : null;

  return (
    <div>
      <div className="page-header">
        <h1>{event.symbol}</h1>
        <p>{event.company_name}</p>
      </div>

      <div className="card">
        <h2>Earnings Information</h2>
        <div className="grid grid-4" style={{ gap: 10 }}>
          <div className="stat">
            <span className="stat-label">Earnings date</span>
            <span className="stat-value small">
              {new Date(`${event.earnings_date}T00:00:00`).toLocaleDateString()}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Timing</span>
            <span className="stat-value small">{TIMING_LABELS[event.earnings_time]}</span>
          </div>
          <div className="stat">
            <span className="stat-label">EPS estimate</span>
            <span className="stat-value small">
              {event.eps_estimate ? formatMoney(event.eps_estimate) : "—"}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Market cap</span>
            <span className="stat-value small">
              {event.market_cap ? `$${(Number(event.market_cap) / 1e9).toFixed(1)}B` : "—"}
            </span>
          </div>
        </div>
        <p className="text-sm text-faint" style={{ marginTop: 10, marginBottom: 0 }}>
          {earningsCountdownLabel(event.earnings_date)} · source: {event.source}
        </p>
      </div>

      {!decision ? (
        <div className="card">
          <p className="text-sm text-muted" style={{ margin: 0 }}>
            No AI decision generated yet.
          </p>
        </div>
      ) : (
        <>
          <div className="card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h2 style={{ margin: 0 }}>AI Recommendation</h2>
              {stage && (
                <span className={`pill ${stage === "settled" ? "pill-positive" : "pill-neutral"}`}>
                  {LIFECYCLE_LABELS[stage]}
                </span>
              )}
            </div>
            <div className="grid grid-3" style={{ gap: 10, marginTop: 10 }}>
              <div className="stat">
                <span className="stat-label">Strategy</span>
                <span className="stat-value small">{decision.strategy_type ?? "—"}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Direction</span>
                <span className="stat-value small">{decision.strategy_direction}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Probability</span>
                <span className="stat-value small">
                  {decision.estimated_probability
                    ? formatPlainPercent(decision.estimated_probability, 0)
                    : "Not available"}
                </span>
              </div>
            </div>
            {decision.legs && decision.legs.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <LegsTable legs={decision.legs} />
              </div>
            )}
            {decision.why_this_strategy && decision.why_this_strategy.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>Why This Strategy</div>
                <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                  {decision.why_this_strategy.map((line, i) => (
                    <li key={i} className="text-sm text-muted">
                      {line}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          {thesis && <ThesisCard thesis={thesis} />}

          <EntryStatusCard entries={entries} />
          <SettlementStatusCard settlements={settlements} />
        </>
      )}
    </div>
  );
}
