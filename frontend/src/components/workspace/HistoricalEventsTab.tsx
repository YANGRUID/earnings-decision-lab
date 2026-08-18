import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { useAsync } from "../../hooks/useAsync";
import { ErrorState, LoadingState, EmptyState } from "../StatusStates";
import { formatPercent } from "../../lib/format";
import type { EarningsEventDetail } from "../../types/api";

async function loadEventDetails(ticker: string): Promise<EarningsEventDetail[]> {
  const summaries = await api.listEarnings({ ticker, limit: 12 });
  return Promise.all(summaries.map((s) => api.getEarningsEvent(s.id)));
}

function MoveBar({ event, maxAbsMove }: { event: EarningsEventDetail; maxAbsMove: number }) {
  const move = event.price_reaction?.next_day_move_pct;
  if (move === null || move === undefined) {
    return <span className="text-faint text-sm">no data</span>;
  }
  const magnitude = Math.abs(Number(move));
  const widthPct = maxAbsMove > 0 ? Math.max((magnitude / maxAbsMove) * 100, 4) : 0;
  const positive = Number(move) >= 0;
  return (
    <div className="move-bar-track">
      <div
        className={`move-bar ${positive ? "move-bar-positive" : "move-bar-negative"}`}
        style={{ width: `${widthPct}%` }}
      />
      <span className={`move-bar-label ${positive ? "positive" : "negative"}`}>
        {formatPercent(Number(move), 1)}
      </span>
    </div>
  );
}

export function HistoricalEventsTab({ ticker }: { ticker: string }) {
  const events = useAsync(() => loadEventDetails(ticker), [ticker]);

  if (events.loading) return <LoadingState label="Loading historical earnings events…" />;
  if (events.error) return <ErrorState message={events.error} />;
  if (!events.data) return null;

  const maxAbsMove = Math.max(
    0,
    ...events.data
      .map((e) => e.price_reaction?.next_day_move_pct)
      .filter((m): m is string => m !== null && m !== undefined)
      .map((m) => Math.abs(Number(m))),
  );

  return (
    <div>
      <p className="text-sm text-muted" style={{ marginTop: 0 }}>
        Strictly retrospective — every event below has already been reported. Next-day move
        magnitude is shown relative to {ticker}'s own largest recorded move in this list.
      </p>

      {events.data.length === 0 ? (
        <EmptyState>No earnings events on record for {ticker}.</EmptyState>
      ) : (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>Period</th>
                <th>Date</th>
                <th>EPS</th>
                <th>Next-day move</th>
              </tr>
            </thead>
            <tbody>
              {events.data.map((event) => (
                <tr key={event.id}>
                  <td>
                    <Link to={`/earnings/${event.id}`}>
                      FY{event.fiscal_year} Q{event.fiscal_quarter}
                    </Link>
                  </td>
                  <td className="mono">{event.earnings_date ?? "—"}</td>
                  <td className="mono">{event.result?.actual_eps ?? "—"}</td>
                  <td style={{ minWidth: 160 }}>
                    <MoveBar event={event} maxAbsMove={maxAbsMove} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
