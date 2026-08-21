"""Phase 4.5 -- captures the real, official option exit market for an
already-entered decision_snapshot: what the $2,000 Moderate AI benchmark
could actually have closed its position for, on the first real trading
day after the earnings event. Mirrors services/benchmark_entry_
capture.py exactly, on the closing side -- same plain-function module
convention, same idempotency/no-lookahead/all-or-nothing-legs/honest-
failure discipline, same append-only immutable attempt+per-leg tables.

See PHASE4_5_SETTLEMENT_ARCHITECTURE_REVIEW.md (and its 2026-08-21
addendum) for the full design and the approved decisions this module
implements:

1. No historical reconstruction fallback, ever. This module only ever
   calls options_provider.get_quotes_for_known_contracts() and
   get_underlying_quote() -- both real, live, contemporaneous provider
   calls -- never services/options_reconstruction.py's historical-LAST-
   price path. A missed live exit window produces an honest FAILED
   attempt, exactly like a missed entry window does.
2. return_pct = realized_pnl / EntryCaptureAttempt.net_entry_cash (the
   already-computed, signed initial premium paid/received) -- read via
   the entry_capture_attempt_id FK, never recomputed.
3. r_multiple = realized_pnl / EntryCaptureAttempt.initial_max_risk --
   the already-computed risk-defined capital unit, never recomputed.
   Sizing (quantity/multiplier per leg, contracts overall) is frozen at
   entry and simply carried forward -- this module never calls
   compute_budget_fit() at all.
4. Writes only to the new settlement_capture_attempt/exit_snapshot
   tables (mirroring entry_capture_attempt/entry_snapshot). The older
   settlement_snapshot table (Phase 4.1 scaffold) is never written to by
   this module.
5. Scheduled by services/scheduler.py::run_exit_capture_job, its own
   job at the same 15:55 ET daily cron trigger the entry job uses.

Reuses, never duplicates: analytics/decision/settlement_math.py for
every P&L/return/R-multiple figure this module produces, services/
benchmark_entry_capture.py's own _resolve_underlying() for underlying
timestamp-coherence validation (the rule is identical for entry and
exit -- only which market timestamp it's checked against differs), and
analytics/earnings_timing.py's compute_entry_exit_schedule() for the
no-lookahead timing gate -- the exact same function entry capture and
decision generation already use, not a second implementation of the
same trading-day math.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.decision.settlement_math import (
    SettlementLegInput,
    compute_settlement_totals,
    leg_realized_pnl_per_share,
)
from analytics.earnings_timing import compute_entry_exit_schedule
from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.entry_capture_attempt import EntryCaptureAttempt
from models.entry_snapshot import EntrySnapshot
from models.enums import (
    AnnouncementTime,
    CaptureStatus,
    EarningsTiming,
    MarketDataQuality,
    OptionAction,
)
from models.exit_snapshot import ExitSnapshot
from models.settlement_capture_attempt import SettlementCaptureAttempt
from providers.base import OptionsDataProvider
from providers.types import KnownContract, OptionQuote
from services.benchmark_entry_capture import (  # noqa: PLC2701 -- shared private helper, coherence check identical for entry and exit
    _resolve_underlying,
)
from services.decision_pipeline import LATE_CUTOFF_GRACE

log = logging.getLogger("services.benchmark_exit_capture")

_TIMING_TO_ANNOUNCEMENT_TIME: dict[EarningsTiming, AnnouncementTime] = {
    EarningsTiming.BMO: AnnouncementTime.BEFORE_MARKET,
    EarningsTiming.AMC: AnnouncementTime.AFTER_MARKET,
    EarningsTiming.DMH: AnnouncementTime.UNKNOWN,
    EarningsTiming.UNKNOWN: AnnouncementTime.UNKNOWN,
}

# Same magnitude and reasoning as benchmark_entry_capture.py's own
# EARLY_CAPTURE_TOLERANCE, kept as its own named constant rather than
# imported: the two bound conceptually different windows (entry vs.
# exit timestamp) and may need to diverge in the future.
EXIT_EARLY_CAPTURE_TOLERANCE = timedelta(minutes=5)


def _map_timing(session: EarningsTiming) -> AnnouncementTime:
    return _TIMING_TO_ANNOUNCEMENT_TIME[session]


@dataclass
class _ExitLegQuote:
    entry_snapshot: EntrySnapshot
    quote: OptionQuote | None
    error: str | None = None
    benchmark_exit_price: Decimal | None = None
    pricing_assumption: str | None = None


def _find_existing_captured_settlement(
    db: Session, decision_snapshot: DecisionSnapshot, portfolio: BenchmarkPortfolio
) -> SettlementCaptureAttempt | None:
    return (
        db.query(SettlementCaptureAttempt)
        .filter_by(
            decision_snapshot_id=decision_snapshot.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.CAPTURED,
        )
        .one_or_none()
    )


def _find_operative_entry_attempt(
    db: Session, decision_snapshot: DecisionSnapshot, portfolio: BenchmarkPortfolio
) -> EntryCaptureAttempt | None:
    return (
        db.query(EntryCaptureAttempt)
        .filter_by(
            decision_snapshot_id=decision_snapshot.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.CAPTURED,
        )
        .one_or_none()
    )


def _verify_no_lookahead(
    decision_snapshot: DecisionSnapshot, now: datetime
) -> tuple[bool, str | None]:
    """The exit-side no-lookahead window: a real window on both sides of
    the scheduled exit_timestamp (EXIT_EARLY_CAPTURE_TOLERANCE ..
    LATE_CUTOFF_GRACE), mirroring benchmark_entry_capture.py's own
    entry-side check exactly. A materially early capture doesn't yet
    represent a full session's post-earnings reaction; a materially late
    one has no honest live path to fall back to at all (Phase 4.5
    approved decision 1 -- no historical reconstruction), so it must
    simply fail."""
    calendar_event = decision_snapshot.earnings_calendar_event
    schedule = compute_entry_exit_schedule(
        calendar_event.earnings_date, _map_timing(calendar_event.earnings_time)
    )

    if now < schedule.exit_timestamp - EXIT_EARLY_CAPTURE_TOLERANCE:
        return False, (
            f"capture time ({now.isoformat()}) is before the valid post-earnings exit window "
            f"({schedule.exit_timestamp.isoformat()} - {EXIT_EARLY_CAPTURE_TOLERANCE}) -- "
            "refusing to capture a benchmark exit materially earlier than the scheduled exit, "
            "which would not represent a full session's post-earnings reaction"
        )
    if now > schedule.exit_timestamp + LATE_CUTOFF_GRACE:
        return False, (
            f"capture time ({now.isoformat()}) is past the valid post-earnings exit window "
            f"({schedule.exit_timestamp.isoformat()} + {LATE_CUTOFF_GRACE}) -- no honest live "
            "quote can represent a day that has already closed, and this module never falls "
            "back to historical reconstruction (Phase 4.5 approved decision 1)"
        )
    return True, None


def _match_exit_quotes(
    entry_legs: list[EntrySnapshot], quotes: list[OptionQuote]
) -> list[_ExitLegQuote]:
    by_conid = {q.external_contract_id: q for q in quotes if q.external_contract_id is not None}
    results: list[_ExitLegQuote] = []
    for entry_leg in entry_legs:
        quote = (
            by_conid.get(entry_leg.external_contract_id)
            if entry_leg.external_contract_id is not None
            else None
        )
        results.append(
            _ExitLegQuote(
                entry_snapshot=entry_leg,
                quote=quote,
                error=None if quote is not None else "no quote found for this contract",
            )
        )
    return results


def _price_exit_leg(leg: _ExitLegQuote) -> None:
    """The conservative, executable-side close -- BID for a long (BUY)
    leg being sold to close, ASK for a short (SELL) leg being bought to
    close. Never the midpoint. Mutates ``leg`` in place with the result
    or an honest error."""
    if leg.quote is None:
        return
    if leg.entry_snapshot.benchmark_entry_price is None:
        leg.error = "entry leg has no captured entry price to diff against"
        return
    if leg.entry_snapshot.action == OptionAction.BUY:
        if leg.quote.bid is None:
            leg.error = "no bid quote available to close a long leg"
            return
        leg.benchmark_exit_price = leg.quote.bid
        leg.pricing_assumption = "SELL_TO_CLOSE_AT_BID"
    else:
        if leg.quote.ask is None:
            leg.error = "no ask quote available to close a short leg"
            return
        leg.benchmark_exit_price = leg.quote.ask
        leg.pricing_assumption = "BUY_TO_CLOSE_AT_ASK"


def capture_benchmark_exit(
    db: Session,
    *,
    decision_snapshot: DecisionSnapshot,
    portfolio: BenchmarkPortfolio,
    options_provider: OptionsDataProvider,
    now: datetime | None = None,
) -> SettlementCaptureAttempt:
    """The one real entry point. Idempotent: an existing CAPTURED
    settlement for this (decision_snapshot, portfolio) pair is returned
    as-is, no new attempt or provider call. Every other outcome --
    no real entry to close, no-lookahead violation, provider failure,
    missing leg quote -- produces exactly one new, honest, immutable
    SettlementCaptureAttempt row (status=FAILED) and is never silently
    retried within this call; retrying is the caller's decision
    (services/scheduler.py-style orchestration), matching entry
    capture's own precedent.
    """
    now = now or datetime.now(UTC)

    existing = _find_existing_captured_settlement(db, decision_snapshot, portfolio)
    if existing is not None:
        return existing

    entry_attempt = _find_operative_entry_attempt(db, decision_snapshot, portfolio)
    if entry_attempt is None:
        attempt = SettlementCaptureAttempt(
            decision_snapshot_id=decision_snapshot.id,
            benchmark_portfolio_id=portfolio.id,
            entry_capture_attempt_id=None,
            status=CaptureStatus.FAILED,
            capture_error="no official (CAPTURED) entry exists for this decision -- nothing to "
            "close",
        )
        db.add(attempt)
        db.flush()
        return attempt

    ok, reason = _verify_no_lookahead(decision_snapshot, now)
    if not ok:
        attempt = SettlementCaptureAttempt(
            decision_snapshot_id=decision_snapshot.id,
            benchmark_portfolio_id=portfolio.id,
            entry_capture_attempt_id=entry_attempt.id,
            status=CaptureStatus.FAILED,
            capture_error=reason,
        )
        db.add(attempt)
        db.flush()
        return attempt

    entry_legs = (
        db.query(EntrySnapshot)
        .filter_by(capture_attempt_id=entry_attempt.id)
        .order_by(EntrySnapshot.leg_index)
        .all()
    )
    if not entry_legs:
        attempt = SettlementCaptureAttempt(
            decision_snapshot_id=decision_snapshot.id,
            benchmark_portfolio_id=portfolio.id,
            entry_capture_attempt_id=entry_attempt.id,
            status=CaptureStatus.FAILED,
            capture_error="operative entry attempt has no legs on record to close",
        )
        db.add(attempt)
        db.flush()
        return attempt

    contracts = [
        KnownContract(
            strike=leg.strike,
            option_type=leg.option_type.value,
            external_contract_id=leg.external_contract_id,
        )
        for leg in entry_legs
        if leg.strike is not None
        and leg.option_type is not None
        and leg.external_contract_id is not None
    ]

    try:
        quotes = options_provider.get_quotes_for_known_contracts(
            decision_snapshot.ticker,
            contracts,
            decision_snapshot.selected_expiration,  # type: ignore[arg-type]
            now,
        )
        # Fetched in the same try/except as the option quotes, mirroring
        # entry capture's own precedent: a real provider failure
        # fetching either one must produce the same honest FAILED
        # attempt, never a partial capture.
        underlying_quote = options_provider.get_underlying_quote(decision_snapshot.ticker)
    except Exception as exc:
        log.warning(
            "benchmark exit capture: provider call failed for decision_snapshot_id=%s",
            decision_snapshot.id,
            exc_info=True,
        )
        attempt = SettlementCaptureAttempt(
            decision_snapshot_id=decision_snapshot.id,
            benchmark_portfolio_id=portfolio.id,
            entry_capture_attempt_id=entry_attempt.id,
            status=CaptureStatus.FAILED,
            capture_error=f"options provider call failed: {exc}",
        )
        db.add(attempt)
        db.flush()
        return attempt

    leg_quotes = _match_exit_quotes(entry_legs, quotes)
    for leg in leg_quotes:
        _price_exit_leg(leg)

    exit_market_timestamp = quotes[0].snapshot_timestamp if quotes else now

    underlying_price, underlying_bid, underlying_ask, underlying_timestamp, underlying_error = (
        _resolve_underlying(underlying_quote, exit_market_timestamp)
    )

    failure_reason: str | None = None
    if underlying_error is not None:
        failure_reason = underlying_error
    else:
        failed_legs = [leg for leg in leg_quotes if leg.error is not None]
        if failed_legs:

            def _leg_label(leg: _ExitLegQuote) -> str:
                option_type = leg.entry_snapshot.option_type
                return f"{option_type.value if option_type else '?'} {leg.entry_snapshot.strike}"

            failure_reason = "; ".join(
                f"leg {leg.entry_snapshot.leg_index} ({_leg_label(leg)}): {leg.error}"
                for leg in failed_legs
            )

    totals = None
    if failure_reason is None and entry_attempt.net_entry_cash is not None:
        settlement_inputs = [
            SettlementLegInput(
                action=leg.entry_snapshot.action,  # type: ignore[arg-type]
                entry_price=leg.entry_snapshot.benchmark_entry_price,  # type: ignore[arg-type]
                exit_price=leg.benchmark_exit_price,  # type: ignore[arg-type]
                quantity=leg.entry_snapshot.quantity,  # type: ignore[arg-type]
                multiplier=leg.entry_snapshot.multiplier,  # type: ignore[arg-type]
            )
            for leg in leg_quotes
        ]
        totals = compute_settlement_totals(
            settlement_inputs,
            contracts=entry_attempt.contracts or 0,
            net_entry_cash=entry_attempt.net_entry_cash,
            initial_max_risk=entry_attempt.initial_max_risk,
        )
    elif failure_reason is None:
        failure_reason = "operative entry attempt has no net_entry_cash on record to settle against"

    status = CaptureStatus.FAILED if failure_reason is not None else CaptureStatus.CAPTURED

    attempt = SettlementCaptureAttempt(
        decision_snapshot_id=decision_snapshot.id,
        benchmark_portfolio_id=portfolio.id,
        entry_capture_attempt_id=entry_attempt.id,
        status=status,
        capture_error=failure_reason,
        underlying_price=underlying_price,
        underlying_bid=underlying_bid,
        underlying_ask=underlying_ask,
        underlying_timestamp=underlying_timestamp,
        exit_market_timestamp=exit_market_timestamp,
        net_exit_price_per_share=(totals.net_exit_price_per_share if totals is not None else None),
        net_exit_cash=(totals.net_exit_cash if totals is not None else None),
        realized_pnl=(totals.realized_pnl if totals is not None else None),
        return_pct=(totals.return_pct if totals is not None else None),
        r_multiple=(totals.r_multiple if totals is not None else None),
        is_win=(totals.is_win if totals is not None else None),
        source_provider=quotes[0].source_provider if quotes else None,
        captured_at=now if status == CaptureStatus.CAPTURED else None,
    )
    db.add(attempt)
    db.flush()

    for leg in leg_quotes:
        quote = leg.quote
        entry_leg = leg.entry_snapshot
        realized_pnl_per_share = (
            None
            if leg.error is not None
            or entry_leg.action is None
            or entry_leg.benchmark_entry_price is None
            or leg.benchmark_exit_price is None
            else leg_realized_pnl_per_share(
                entry_leg.action, entry_leg.benchmark_entry_price, leg.benchmark_exit_price
            )
        )
        db.add(
            ExitSnapshot(
                decision_id=decision_snapshot.id,
                settlement_attempt_id=attempt.id,
                entry_snapshot_id=entry_leg.id,
                leg_index=entry_leg.leg_index,
                status=CaptureStatus.CAPTURED if leg.error is None else CaptureStatus.FAILED,
                captured_at=now if leg.error is None else None,
                external_contract_id=entry_leg.external_contract_id,
                expiration=entry_leg.expiration,
                strike=entry_leg.strike,
                option_type=entry_leg.option_type,
                action=entry_leg.action,
                quantity=entry_leg.quantity,
                multiplier=entry_leg.multiplier,
                bid=quote.bid if quote else None,
                ask=quote.ask if quote else None,
                mid=(
                    (quote.bid + quote.ask) / 2
                    if quote and quote.bid is not None and quote.ask is not None
                    else None
                ),
                last_price=quote.last_price if quote else None,
                implied_volatility=quote.implied_volatility if quote else None,
                delta=quote.delta if quote else None,
                gamma=quote.gamma if quote else None,
                theta=quote.theta if quote else None,
                vega=quote.vega if quote else None,
                market_data_quality=(
                    MarketDataQuality(quote.market_data_quality)
                    if quote and quote.market_data_quality
                    else None
                ),
                pricing_source=quote.source_provider if quote else None,
                benchmark_exit_price=leg.benchmark_exit_price,
                pricing_assumption=leg.pricing_assumption,
                realized_pnl_per_share=realized_pnl_per_share,
                capture_error=leg.error,
                source_provider=quote.source_provider if quote else None,
            )
        )
    db.flush()

    log.info(
        "benchmark exit capture: decision_snapshot_id=%s status=%s reason=%s",
        decision_snapshot.id,
        status.value,
        failure_reason,
    )
    return attempt
