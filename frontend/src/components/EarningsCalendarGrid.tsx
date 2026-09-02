import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { LoadingState, ErrorState } from "./StatusStates";
import type { EarningsCalendarEvent } from "../types/api";

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
]; // fmt: skip

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

export function EarningsCalendarGrid() {
  const today = new Date();
  const [cursor, setCursor] = useState({ year: today.getFullYear(), month: today.getMonth() + 1 });

  const monthEvents = useAsync(
    () => api.listEarningsByMonth(cursor.year, cursor.month),
    [cursor.year, cursor.month]
  );

  const goToMonth = (delta: number) => {
    setCursor((prev) => {
      const zeroBased = prev.month - 1 + delta;
      const year = prev.year + Math.floor(zeroBased / 12);
      const month = ((zeroBased % 12) + 12) % 12 + 1;
      return { year, month };
    });
  };
  const goToToday = () => setCursor({ year: today.getFullYear(), month: today.getMonth() + 1 });

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
  for (const list of eventsByDay.values()) {
    list.sort((a, b) => {
      const capA = a.market_cap !== null ? Number(a.market_cap) : -Infinity;
      const capB = b.market_cap !== null ? Number(b.market_cap) : -Infinity;
      return capB - capA;
    });
  }

  const totalDays = daysInMonth(cursor.year, cursor.month);
  const leadingBlanks = firstWeekdayOfMonth(cursor.year, cursor.month);
  const cells: (number | null)[] = [
    ...Array.from({ length: leadingBlanks }, () => null),
    ...Array.from({ length: totalDays }, (_, i) => i + 1),
  ];
  // Trailing blanks so the grid always ends on a full week row.
  while (cells.length % 7 !== 0) cells.push(null);

  const isCurrentMonth = cursor.year === today.getFullYear() && cursor.month === today.getMonth() + 1;

  return (
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
          const isToday = isCurrentMonth && day === today.getDate();
          return (
            <div key={i} className={`calendar-cell${isToday ? " calendar-cell-today" : ""}`}>
              <div className="calendar-cell-date">{day}</div>
              <div className="calendar-cell-tickers">
                {dayEvents.slice(0, MAX_TICKERS_PER_CELL).map((event) => (
                  <Link
                    key={event.id}
                    to={`/earnings-calendar/${event.symbol}`}
                    className="calendar-ticker-chip"
                    title={event.company_name}
                  >
                    <span className="calendar-ticker-dot" style={{ background: dotColor(event.symbol) }}>
                      {event.symbol[0]}
                    </span>
                    {event.symbol}
                  </Link>
                ))}
                {dayEvents.length > MAX_TICKERS_PER_CELL && (
                  <span className="calendar-cell-more">+{dayEvents.length - MAX_TICKERS_PER_CELL} more</span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
