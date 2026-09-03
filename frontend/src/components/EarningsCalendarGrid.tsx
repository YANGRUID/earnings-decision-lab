import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { useListControls } from "../hooks/useListControls";
import { ListToolbar, Pager } from "./ListControls";
import { LoadingState, ErrorState } from "./StatusStates";
import { fmtMarketCap } from "./v4/shared";
import { formatEt, stateLabel } from "../lib/operationsFormat";
import type { EarningsCalendarEvent, PipelineEvent } from "../types/api";

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
]; // fmt: skip

const TIMING_LABELS: Record<string, string> = {
  bmo: "Before market open",
  amc: "After market close",
  dmh: "During market hours",
  unknown: "Timing unknown",
};

// A fixed, deterministic display palette -- purely cosmetic (which color a
// ticker's dot gets), never a fact about the company itself, so picking it
// from a hash of the real symbol (not a lookup, not fabricated data) is
// honest: two renders of the same real ticker always get the same color.
const DOT_PALETTE = [
  "#f59e0b", "#ef4444", "#8b5cf6", "#3b82f6",
  "#10b981", "#ec4899", "#06b6d4", "#84cc16",
]; // fmt: skip

function dotColor(symbol: string): string {
  let hash = 0;
  for (let i = 0; i < symbol.length; i++) hash = (hash * 31 + symbol.charCodeAt(i)) % DOT_PALETTE.length;
  return DOT_PALETTE[Math.abs(hash) % DOT_PALETTE.length];
}

const MAX_TICKERS_PER_CELL = 4;

/** Real calendar-day arithmetic only -- Date's own UTC-safe constructors,
 * never a string-parsed guess. `month` is 1-12 (not JS's native 0-11) to
 * match the backend's own GET /earnings-calendar/by-month?month= contract. */
function daysInMonth(year: number, month: number): number {
  return new Date(year, month, 0).getDate();
}

function firstWeekdayOfMonth(year: number, month: number): number {
  return new Date(year, month - 1, 1).getDay();
}

function capOf(event: EarningsCalendarEvent): number {
  return event.market_cap !== null ? Number(event.market_cap) : -Infinity;
}

function isoDate(year: number, month: number, day: number): string {
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function longDate(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function fmtEstimate(value: string | null, kind: "eps" | "revenue"): string {
  if (value === null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
  if (kind === "eps") return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return fmtMarketCap(value);
}

/** Every company reporting on one calendar day, with its V4 pipeline state
 * where the event lies inside the V4 window (today−2 … today+7). Opened by
 * clicking a ticker, a day number or the "+N more" overflow in the grid. */
function CalendarDayTable({
  date,
  events,
  pipeline,
  highlight,
  onClose,
}: {
  date: string;
  events: EarningsCalendarEvent[];
  pipeline: Map<string, PipelineEvent>;
  highlight: string | null;
  onClose: () => void;
}) {
  const controls = useListControls<EarningsCalendarEvent>({
    rows: events,
    urlKey: "cal",
    searchKeys: [(e) => e.symbol, (e) => e.company_name],
    facet: { label: "Timing", getValue: (e) => e.earnings_time, format: (v) => TIMING_LABELS[v] ?? v },
    sorts: [
      { key: "cap", label: "Market cap (largest first)", compare: (a, b) => capOf(b) - capOf(a) },
      { key: "ticker", label: "Ticker (A–Z)", compare: (a, b) => a.symbol.localeCompare(b.symbol) },
      { key: "timing", label: "Timing", compare: (a, b) => a.earnings_time.localeCompare(b.earnings_time) || capOf(b) - capOf(a) },
    ],
    defaultSort: "cap",
    defaultPageSize: 100,
  });
  const activeRow = useRef<HTMLTableRowElement | null>(null);
  useEffect(() => {
    activeRow.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [highlight, date]);
  const inWindow = events.some((e) => pipeline.has(`${e.symbol}|${e.earnings_date}`));

  return (
    <div className="card calendar-day" data-testid="calendar-day-table">
      <div className="calendar-day-header">
        <div>
          <div className="eyebrow-label">Earnings on</div>
          <h2 style={{ margin: 0 }}>{longDate(date)}</h2>
          <div className="text-sm text-muted">
            {events.length} {events.length === 1 ? "company" : "companies"} · click a ticker to open its workspace
            {inWindow ? " · V4 pipeline state shown for events inside the 15:30 ET window" : ""}
          </div>
        </div>
        <button className="btn-secondary" onClick={onClose} aria-label="Close day view">Close ×</button>
      </div>
      <ListToolbar controls={controls} placeholder="Search ticker or company" testId="calendar-day-controls" />
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Company</th>
              <th>Timing</th>
              <th>Market cap</th>
              <th>EPS est.</th>
              <th>Revenue est.</th>
              <th>Decision (ET)</th>
              <th>Settlement (ET)</th>
              <th>V4 pipeline</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {controls.visible.map((e) => {
              const p = pipeline.get(`${e.symbol}|${e.earnings_date}`) ?? null;
              const active = highlight === e.symbol;
              return (
                <tr
                  key={e.id}
                  ref={active ? activeRow : undefined}
                  className={active ? "calendar-day-row--active" : undefined}
                  data-symbol={e.symbol}
                >
                  <td className="mono"><Link to={`/company/${e.symbol}`}>{e.symbol}</Link></td>
                  <td>{e.company_name}</td>
                  <td className="text-sm">{TIMING_LABELS[e.earnings_time] ?? e.earnings_time}</td>
                  <td className="mono">{fmtMarketCap(e.market_cap)}</td>
                  <td className="mono">{fmtEstimate(e.eps_estimate, "eps")}</td>
                  <td className="mono">{fmtEstimate(e.revenue_estimate, "revenue")}</td>
                  <td className="mono text-sm">{p ? formatEt(p.entry_timestamp) : "—"}</td>
                  <td className="mono text-sm">{p ? formatEt(p.exit_timestamp) : "—"}</td>
                  <td>
                    {p ? (
                      <>
                        <span className={`pill pill-${p.shadow_decision_id ? "positive" : p.lifecycle_state === "BUSINESS_INELIGIBLE" ? "neutral" : "warning"}`} title={p.lifecycle_reason ?? undefined}>{stateLabel(p.lifecycle_state)}</span>
                        {p.shadow_decision_id && (
                          <>
                            {" "}
                            <Link className="text-link text-sm" to={`/v4-decision-lab/${p.shadow_decision_id}`}>decision →</Link>
                          </>
                        )}
                      </>
                    ) : (
                      <span className="text-faint text-sm">outside the V4 window</span>
                    )}
                  </td>
                  <td className="text-sm text-muted">{e.source}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Pager controls={controls} testId="calendar-day-pager" />
    </div>
  );
}

export function EarningsCalendarGrid() {
  const today = new Date();
  const [cursor, setCursor] = useState({ year: today.getFullYear(), month: today.getMonth() + 1 });
  const [selected, setSelected] = useState<{ date: string; symbol: string | null } | null>(null);

  const monthEvents = useAsync(
    () => api.listEarningsByMonth(cursor.year, cursor.month),
    [cursor.year, cursor.month]
  );
  // The complete V4 window (including past rows) so a day table can state the
  // pipeline position of every event that has one. One request, abortable.
  const pipeline = useAsync((signal) => api.getOperationsEvents({ signal, includePast: true }), []);
  const pipelineByKey = new Map<string, PipelineEvent>();
  for (const p of pipeline.data?.events ?? []) pipelineByKey.set(`${p.symbol}|${p.earnings_date}`, p);

  const goToMonth = (delta: number) => {
    setSelected(null);
    setCursor((prev) => {
      const zeroBased = prev.month - 1 + delta;
      const year = prev.year + Math.floor(zeroBased / 12);
      const month = ((zeroBased % 12) + 12) % 12 + 1;
      return { year, month };
    });
  };
  const goToToday = () => {
    setSelected(null);
    setCursor({ year: today.getFullYear(), month: today.getMonth() + 1 });
  };

  if (monthEvents.loading && !monthEvents.data) return <LoadingState label="Loading earnings calendar…" />;
  if (monthEvents.error && !monthEvents.data) return <ErrorState message={monthEvents.error} />;

  const eventsByDay = new Map<number, EarningsCalendarEvent[]>();
  for (const event of monthEvents.data ?? []) {
    const day = Number(event.earnings_date.slice(8, 10));
    const list = eventsByDay.get(day) ?? [];
    list.push(event);
    eventsByDay.set(day, list);
  }
  // Largest market cap first within each day, so the MAX_TICKERS_PER_CELL
  // names actually shown are the most significant ones -- the "+N more"
  // overflow is the smaller-cap remainder, not an arbitrary API-order cut.
  for (const list of eventsByDay.values()) list.sort((a, b) => capOf(b) - capOf(a));

  const totalDays = daysInMonth(cursor.year, cursor.month);
  const leadingBlanks = firstWeekdayOfMonth(cursor.year, cursor.month);
  const cells: (number | null)[] = [
    ...Array.from({ length: leadingBlanks }, () => null),
    ...Array.from({ length: totalDays }, (_, i) => i + 1),
  ];
  // Trailing blanks so the grid always ends on a full week row.
  while (cells.length % 7 !== 0) cells.push(null);

  const isCurrentMonth = cursor.year === today.getFullYear() && cursor.month === today.getMonth() + 1;
  const selectedEvents = selected
    ? (monthEvents.data ?? []).filter((e) => e.earnings_date === selected.date)
    : [];
  const open = (date: string, symbol: string | null) => setSelected({ date, symbol });

  return (
    <div>
      <div className="card">
        <div className="calendar-nav">
          <button className="calendar-nav-btn" onClick={() => goToMonth(-1)} aria-label="Previous month">
            ‹
          </button>
          <div className="calendar-nav-title">
            {MONTH_NAMES[cursor.month - 1]} {cursor.year}
          </div>
          <div className="calendar-nav-controls">
            <button className="calendar-nav-btn" onClick={goToToday} disabled={isCurrentMonth}>
              Today
            </button>
            <button className="calendar-nav-btn" onClick={() => goToMonth(1)} aria-label="Next month">
              ›
            </button>
          </div>
        </div>

        <div className="calendar-grid-weekdays">
          {WEEKDAYS.map((day) => (
            <div key={day} className="calendar-weekday">
              {day}
            </div>
          ))}
        </div>
        <div className="calendar-grid-body">
          {cells.map((day, i) => {
            if (day === null) return <div key={i} className="calendar-cell calendar-cell-empty" />;
            const dayEvents = eventsByDay.get(day) ?? [];
            const date = isoDate(cursor.year, cursor.month, day);
            const isToday = isCurrentMonth && day === today.getDate();
            const isSelected = selected?.date === date;
            return (
              <div
                key={i}
                className={`calendar-cell${isToday ? " calendar-cell-today" : ""}${isSelected ? " calendar-cell-selected" : ""}`}
                data-date={date}
              >
                {dayEvents.length > 0 ? (
                  <button className="calendar-cell-date calendar-cell-date-btn" onClick={() => open(date, null)} title={`Show all ${dayEvents.length} companies reporting on ${date}`}>
                    {day}
                  </button>
                ) : (
                  <div className="calendar-cell-date">{day}</div>
                )}
                <div className="calendar-cell-tickers">
                  {dayEvents.slice(0, MAX_TICKERS_PER_CELL).map((event) => (
                    <button
                      key={event.id}
                      type="button"
                      className="calendar-ticker-chip"
                      title={`${event.company_name} — ${TIMING_LABELS[event.earnings_time] ?? event.earnings_time}`}
                      onClick={() => open(date, event.symbol)}
                    >
                      <span className="calendar-ticker-dot" style={{ background: dotColor(event.symbol) }}>
                        {event.symbol[0]}
                      </span>
                      {event.symbol}
                    </button>
                  ))}
                  {dayEvents.length > MAX_TICKERS_PER_CELL && (
                    <button type="button" className="calendar-cell-more" onClick={() => open(date, null)}>
                      +{dayEvents.length - MAX_TICKERS_PER_CELL} more
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
      {selected && (
        <CalendarDayTable
          date={selected.date}
          events={selectedEvents}
          pipeline={pipelineByKey}
          highlight={selected.symbol}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}
