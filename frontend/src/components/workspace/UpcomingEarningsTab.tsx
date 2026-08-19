import { useState } from "react";
import { Link } from "react-router-dom";
import { useAsync } from "../../hooks/useAsync";
import { api, ApiError } from "../../api/client";
import { formatMoney, formatPercent, providerLabel } from "../../lib/format";
import { LoadingState } from "../StatusStates";
import type { OptionsMarketState, ResearchOverview } from "../../types/api";

function formatPlainPercent(value: string | null): string {
  if (value === null) return "—";
  return formatPercent(Number(value), 1);
}

const DATE_SOURCE_LABEL: Record<string, string> = {
  alpha_vantage: "provider-confirmed",
  manual: "manually entered by owner",
  estimated: "estimated, not confirmed",
  unknown: "unknown provenance",
};

function availability(has: boolean): string {
  return has ? "Available" : "Unavailable";
}

/** "Current" must mean the snapshot is actually from the live/in-progress
 * session, not merely "wasn't a deliberate fallback" -- is_fallback_snapshot
 * is false for a company's only/latest snapshot even when that snapshot is
 * hours or days old (real case: AAPL, 2026-08-19, 11h54m-old snapshot was
 * showing "Current" here while Strategy Lab correctly said "previous
 * session" for the same data). actionability is the authoritative,
 * session-date-aware signal -- see services/options_analytics.py. */
function pricingSnapshotLabel(market: OptionsMarketState): string {
  if (market.actionability === "actionable_current") return "Current";
  if (market.actionability === "actionable_previous_session") {
    if (market.snapshot_purpose === "close") return "Previous close";
    if (market.snapshot_purpose === "reconstructed_close") {
      return "Previous close (reconstructed from IBKR historical data)";
    }
    return "Previous session";
  }
  if (market.actionability === "stale_research_only") return "Stale (previous session)";
  return "Not currently priceable";
}

/** The canonical options-market state for every case where an implied move
 * *isn't* available -- see services/options_analytics.py::OptionsMarketState.
 * Never collapses "no chain at all" / "chain but no bid-ask" / "chain with
 * IV+Greeks but no priceable premium" / "stale" / "not earnings-anchored"
 * into one generic "no option data" message (that collapse is exactly the
 * bug that let this tab say "no options data" for AVGO while Strategy Lab
 * showed a real 22-contract chain). */
function OptionsMarketStateCard({ market }: { market: OptionsMarketState }) {
  return (
    <>
      <div className="grid grid-2" style={{ gap: 10 }}>
        <div className="stat">
          <span className="stat-label">Option chain</span>
          <span className="stat-value small">
            {market.chain_exists ? `Available · ${market.contract_count} contracts` : "Not collected"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Expiration</span>
          <span className="stat-value small mono">{market.expiration ?? "—"}</span>
        </div>
        {market.chain_exists && (
          <>
            <div className="stat">
              <span className="stat-label">Pricing snapshot</span>
              <span className="stat-value small">{pricingSnapshotLabel(market)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Data quality</span>
              <span className="stat-value small mono">{market.market_data_quality ?? "unknown"}</span>
            </div>
            <div className="stat">
              <span className="stat-label">IV / Greeks</span>
              <span className="stat-value small">{availability(market.has_iv || market.has_greeks)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Bid / Ask</span>
              <span className="stat-value small">{availability(market.has_bid_ask)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Implied move</span>
              <span className="stat-value small">Cannot be calculated from the current snapshot</span>
            </div>
          </>
        )}
      </div>
      {market.chain_exists && (
        <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
          Snapshot from {market.source ? providerLabel(market.source) : "an unknown source"}
          {market.snapshot_age_label ? `, ${market.snapshot_age_label} ago` : ""}. {market.reason}
        </p>
      )}
      {!market.chain_exists && (
        <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
          {market.reason}
        </p>
      )}
    </>
  );
}

function ManualEarningsDateOverride({
  ticker,
  onSaved,
}: {
  ticker: string;
  onSaved: () => void;
}) {
  const [reportDate, setReportDate] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!reportDate) return;
    setSaving(true);
    setError(null);
    try {
      await api.setEarningsDateOverride(ticker, { estimated_report_date: reportDate });
      setReportDate("");
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save this date.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <details className="card" style={{ marginTop: 12 }}>
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>
        Owner override: set next earnings date manually
      </summary>
      <div style={{ marginTop: 14 }}>
        <p className="text-sm text-muted" style={{ marginTop: 0 }}>
          For when no provider has published {ticker}'s next report date yet. Stored as a
          manually entered date — never shown as provider-confirmed.
        </p>
        <div className="field">
          <label>Estimated report date</label>
          <input
            type="date"
            value={reportDate}
            onChange={(e) => setReportDate(e.target.value)}
          />
        </div>
        <button className="btn" disabled={saving || !reportDate} onClick={submit}>
          {saving ? "Saving…" : "Save date"}
        </button>
        {error && <div className="notice" style={{ marginTop: 10 }}>{error}</div>}
      </div>
    </details>
  );
}

export function UpcomingEarningsTab({
  ticker,
  overview,
  onOverviewChanged,
}: {
  ticker: string;
  overview: ResearchOverview;
  onOverviewChanged: () => void;
}) {
  const replay = useAsync(() => api.getReplaySummary(), []);
  const est = overview.latest_earnings_estimate;
  const iv = overview.latest_volatility_snapshot;
  const companyReplay = replay.data?.companies.find((c) => c.company.ticker === ticker);
  const hist = companyReplay?.historical_moves ?? null;

  return (
    <div>
      <p className="text-sm text-muted" style={{ marginTop: 0 }}>
        What the market currently expects for {ticker}'s <strong>next, unreported</strong>{" "}
        earnings report — separate from any specific past event. See{" "}
        <Link className="text-link" to="#historical">
          Historical Events
        </Link>{" "}
        for what actually happened before.
      </p>

      <div className="grid grid-2">
        <div className="card">
          <h2>Market expectations</h2>
          {est ? (
            <>
              <div className="grid grid-2" style={{ gap: 10 }}>
                <div className="stat">
                  <span className="stat-label">EPS estimate (avg, {est.horizon})</span>
                  <span className="stat-value small">{formatMoney(est.eps_estimate_average)}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">EPS revision trend</span>
                  <span className="stat-value small mono">{est.eps_revision_direction}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">Analyst count (EPS)</span>
                  <span className="stat-value small">{est.eps_estimate_analyst_count ?? "—"}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">Revenue estimate (avg)</span>
                  <span className="stat-value small">
                    {est.revenue_estimate_average
                      ? `$${(Number(est.revenue_estimate_average) / 1e9).toFixed(2)}B`
                      : "—"}
                  </span>
                </div>
              </div>
              <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
                Estimated report date: {est.estimated_report_date ?? "unknown"} (
                {DATE_SOURCE_LABEL[est.date_source] ?? est.date_source}) · consensus as of{" "}
                {new Date(est.snapshot_timestamp).toLocaleDateString()} ({est.source_provider}).
              </p>
            </>
          ) : (
            <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
              No real analyst-consensus snapshot has been collected yet. Use{" "}
              <span className="mono">Refresh</span> above to fetch one.
            </p>
          )}
          <ManualEarningsDateOverride ticker={ticker} onSaved={onOverviewChanged} />
        </div>

        <div className="card">
          <h2>Options market pricing</h2>
          {iv ? (
            <>
              <div className="grid grid-2" style={{ gap: 10 }}>
                <div className="stat">
                  <span className="stat-label">Implied move</span>
                  <span className="stat-value small">
                    {formatPlainPercent(iv.implied_move_pct)}
                    {iv.implied_move_absolute ? ` ($${formatMoney(iv.implied_move_absolute)})` : ""}
                  </span>
                </div>
                <div className="stat">
                  <span className="stat-label">Expiration used</span>
                  <span className="stat-value small mono">{iv.near_term_expiration ?? "—"}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">ATM IV</span>
                  <span className="stat-value small">{formatPlainPercent(iv.atm_iv_near)}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">Put/call OI ratio</span>
                  <span className="stat-value small">
                    {iv.put_call_open_interest_ratio
                      ? Number(iv.put_call_open_interest_ratio).toFixed(2)
                      : "—"}
                  </span>
                </div>
              </div>
              <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
                Method: {iv.method} · computed {new Date(iv.computed_at).toLocaleString()}.
              </p>
            </>
          ) : (
            <OptionsMarketStateCard market={overview.options_market} />
          )}
        </div>
      </div>

      <div className="card">
        <h2>Historical move compatibility check</h2>
        {replay.loading && <LoadingState label="Loading historical moves…" />}
        {hist ? (
          <>
            <div className="grid grid-2" style={{ gap: 10 }}>
              <div className="stat">
                <span className="stat-label">Average |move| ({hist.sample_size} past events)</span>
                <span className="stat-value small">{formatPlainPercent(hist.average_abs_move_pct)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Largest past move</span>
                <span className="stat-value small">{formatPlainPercent(hist.largest_move_pct_signed)}</span>
              </div>
            </div>
            {iv?.implied_move_pct && (
              <p className="text-sm text-muted" style={{ marginTop: 10, marginBottom: 0 }}>
                The current implied move ({formatPlainPercent(iv.implied_move_pct)}) compares to a
                real historical average of {formatPlainPercent(hist.average_abs_move_pct)} across{" "}
                {hist.sample_size} past reports — not a prediction, just how this compares to what
                actually happened before. See each candidate's own compatibility check in{" "}
                <strong>Strategy Lab</strong>.
              </p>
            )}
          </>
        ) : (
          !replay.loading && (
            <p className="text-sm text-muted" style={{ margin: 0 }}>
              No other reported event for {ticker} with a recorded move yet.
            </p>
          )
        )}
      </div>
    </div>
  );
}
