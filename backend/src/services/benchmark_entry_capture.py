"""Phase 4.4 -- captures the real, official option entry market for a
frozen decision_snapshot: what the $2,000 Moderate AI benchmark could
actually have entered before earnings. This is the
"BenchmarkEntryCaptureService" the phase brief calls for, implemented as
a plain-function module rather than a class -- matching this codebase's
existing services/ convention (no existing service in this project is a
class; see services/earnings_calendar_sync.py, services/earnings_
eligibility.py, services/decision_snapshot_freezing.py for the same
choice made in earlier Phase 4 sub-phases).

Entry data only -- no post-earnings settlement, no P&L, no exit. See
PHASE4.3_ARCHITECTURE_REVIEW.md sec 0A's resolution: DecisionSnapshot
stays immutable forever, including its own ``status`` column. Whether a
decision has been "entered" is derived by querying for an
EntryCaptureAttempt with status=CAPTURED, never by mutating the
snapshot -- see models/entry_capture_attempt.py's own docstring for the
full reasoning (an immutable, append-only attempt record was evaluated
and is sufficient; a genuinely mutable workflow-state table was
considered and rejected as unnecessary).

Reuses, never duplicates: analytics/options/payoff.py's analyze() and
analytics/decision/budget.py's compute_budget_fit() for every dollar
figure this module produces (Phase 4.4 sec 9/10 explicitly forbid a
second, parallel options-math system), services/options_analytics.py's
_latest_close_price_on_or_before() for the underlying-price fallback
(the same cross-module import services/expiration_engine.py and
services/strategy_generation.py already use), services/options_
reconstruction.py's MAX_UNDERLYING_OPTION_SKEW constant for timestamp-
coherence validation, and analytics/earnings_timing.py's
compute_entry_exit_schedule() for the no-lookahead timing gate (Phase
4.4 sec 2/3) -- the exact same function services/decision_pipeline.py
already uses for the decision-generation timing gate, not a second
implementation of the same trading-day math.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.decision.budget import compute_budget_fit
from analytics.decision.risk_profile import DEFAULT_MAX_RISK_UTILIZATION_PCT
from analytics.earnings_timing import compute_entry_exit_schedule
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
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
    OptionType,
)
from providers.base import OptionsDataProvider
from providers.types import OptionQuote
from services.decision_pipeline import LATE_CUTOFF_GRACE
from services.options_analytics import _latest_close_price_on_or_before
from services.options_reconstruction import MAX_UNDERLYING_OPTION_SKEW

log = logging.getLogger("services.benchmark_entry_capture")

_TIMING_TO_ANNOUNCEMENT_TIME: dict[EarningsTiming, AnnouncementTime] = {
    EarningsTiming.BMO: AnnouncementTime.BEFORE_MARKET,
    EarningsTiming.AMC: AnnouncementTime.AFTER_MARKET,
    EarningsTiming.DMH: AnnouncementTime.UNKNOWN,
    EarningsTiming.UNKNOWN: AnnouncementTime.UNKNOWN,
}


@dataclass
class _LegQuote:
    leg_index: int
    option_type: OptionType
    action: OptionAction
    strike: Decimal
    quantity: int
    quote: OptionQuote | None
    error: str | None = None
    benchmark_entry_price: Decimal | None = None
    pricing_assumption: str | None = None


def _map_timing(session: EarningsTiming) -> AnnouncementTime:
    return _TIMING_TO_ANNOUNCEMENT_TIME[session]


def _find_existing_captured_attempt(
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
    """Phase 4.4 sec 3: an official benchmark decision must exist before
    the earnings information becomes public, and the capture itself must
    happen inside the real pre-earnings window -- both checked freshly
    here, never assumed just because Phase 4.3 already checked the first
    one at generation time."""
    calendar_event = decision_snapshot.earnings_calendar_event
    schedule = compute_entry_exit_schedule(
        calendar_event.earnings_date, _map_timing(calendar_event.earnings_time)
    )

    if decision_snapshot.generated_at > schedule.entry_timestamp + LATE_CUTOFF_GRACE:
        return False, (
            f"decision_snapshot.generated_at ({decision_snapshot.generated_at.isoformat()}) is "
            f"past the permitted decision cutoff ({schedule.entry_timestamp.isoformat()} + "
            f"{LATE_CUTOFF_GRACE}) -- refusing to back an official entry with a decision that "
            "may not honestly predate the earnings reaction"
        )
    if now > schedule.entry_timestamp + LATE_CUTOFF_GRACE:
        return False, (
            f"capture time ({now.isoformat()}) is past the valid pre-earnings entry window "
            f"({schedule.entry_timestamp.isoformat()} + {LATE_CUTOFF_GRACE}) -- refusing to "
            "silently backfill an entry after the market may have already reacted"
        )
    return True, None


def _resolve_underlying(
    db: Session, ticker: str, quotes: list[OptionQuote], as_of: datetime
) -> tuple[Decimal | None, datetime | None, str | None]:
    """Real underlying price + timestamp for this capture, or an honest
    reason why neither is available. Mirrors services/options_analytics.py's
    own quotes[0].underlying_price -> _latest_close_price_on_or_before
    fallback exactly (Phase 4.4 sec 7: reuse existing V3 coherence logic,
    do not duplicate the resolver)."""
    live_price = quotes[0].underlying_price if quotes else None
    live_timestamp = quotes[0].underlying_timestamp if quotes else None
    if live_price is not None:
        if live_timestamp is not None and quotes[0].snapshot_timestamp is not None:
            skew = abs(quotes[0].snapshot_timestamp - live_timestamp)
            if skew > MAX_UNDERLYING_OPTION_SKEW:
                return None, None, (
                    f"underlying/option quote timestamp skew ({skew}) exceeds "
                    f"{MAX_UNDERLYING_OPTION_SKEW} -- refusing to combine a stale underlying "
                    "price with a fresh option quote"
                )
        return live_price, live_timestamp, None

    fallback_price = _latest_close_price_on_or_before(db, ticker, as_of.date())
    if fallback_price is None:
        return None, None, "no live or daily-close underlying price available"
    return fallback_price, None, None


def _match_leg_quotes(
    decision_snapshot: DecisionSnapshot, quotes: list[OptionQuote]
) -> list[_LegQuote]:
    legs = decision_snapshot.legs or []
    by_key = {(q.strike, q.option_type): q for q in quotes}
    results: list[_LegQuote] = []
    for idx, leg in enumerate(legs):
        option_type = OptionType(leg["option_type"])
        action = OptionAction(leg["action"])
        strike = Decimal(leg["strike"])
        quantity = int(leg.get("quantity", 1))
        quote = by_key.get((strike, option_type.value))
        results.append(
            _LegQuote(
                leg_index=idx,
                option_type=option_type,
                action=action,
                strike=strike,
                quantity=quantity,
                quote=quote,
                error=None if quote is not None else "no quote found for this contract",
            )
        )
    return results


def _price_leg(leg: _LegQuote) -> None:
    """Phase 4.4 sec 8: the official conservative, executable-side fill
    -- ASK for a long (BUY) leg, BID for a short (SELL) leg. Never the
    midpoint. Mutates ``leg`` in place with the result or an honest
    error."""
    if leg.quote is None:
        return
    if leg.action == OptionAction.BUY:
        if leg.quote.ask is None:
            leg.error = "no ask quote available for a long leg"
            return
        leg.benchmark_entry_price = leg.quote.ask
        leg.pricing_assumption = "BUY_TO_OPEN_AT_ASK"
    else:
        if leg.quote.bid is None:
            leg.error = "no bid quote available for a short leg"
            return
        leg.benchmark_entry_price = leg.quote.bid
        leg.pricing_assumption = "SELL_TO_OPEN_AT_BID"


def capture_benchmark_entry(
    db: Session,
    *,
    decision_snapshot: DecisionSnapshot,
    portfolio: BenchmarkPortfolio,
    options_provider: OptionsDataProvider,
    now: datetime | None = None,
) -> EntryCaptureAttempt:
    """The one real entry point. Idempotent: an existing CAPTURED attempt
    for this (decision_snapshot, portfolio) pair is returned as-is, no
    new attempt or provider call. Every other outcome -- no-lookahead
    violation, provider failure, missing leg, insufficient budget --
    produces exactly one new, honest, immutable EntryCaptureAttempt row
    (status=FAILED) and is never silently retried within this call;
    retrying is the caller's decision (services/decision_pipeline.py-
    style orchestration), matching Phase 4.4 sec 12/15.
    """
    now = now or datetime.now(UTC)

    existing = _find_existing_captured_attempt(db, decision_snapshot, portfolio)
    if existing is not None:
        return existing

    ok, reason = _verify_no_lookahead(decision_snapshot, now)
    if not ok:
        attempt = EntryCaptureAttempt(
            decision_snapshot_id=decision_snapshot.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error=reason,
        )
        db.add(attempt)
        db.flush()
        return attempt

    if not decision_snapshot.legs:
        attempt = EntryCaptureAttempt(
            decision_snapshot_id=decision_snapshot.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error="decision_snapshot has no recommended strategy legs to enter",
        )
        db.add(attempt)
        db.flush()
        return attempt

    try:
        quotes = options_provider.get_option_chain(
            decision_snapshot.ticker, now, expiration=decision_snapshot.selected_expiration
        )
    except Exception as exc:
        log.warning(
            "benchmark entry capture: provider call failed for decision_snapshot_id=%s",
            decision_snapshot.id,
            exc_info=True,
        )
        attempt = EntryCaptureAttempt(
            decision_snapshot_id=decision_snapshot.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error=f"options provider call failed: {exc}",
        )
        db.add(attempt)
        db.flush()
        return attempt

    leg_quotes = _match_leg_quotes(decision_snapshot, quotes)
    for leg in leg_quotes:
        _price_leg(leg)

    underlying_price, underlying_timestamp, underlying_error = _resolve_underlying(
        db, decision_snapshot.ticker, quotes, now
    )

    option_market_timestamp = quotes[0].snapshot_timestamp if quotes else now

    failure_reason: str | None = None
    if underlying_error is not None:
        failure_reason = underlying_error
    else:
        failed_legs = [leg for leg in leg_quotes if leg.error is not None]
        if failed_legs:
            failure_reason = "; ".join(
                f"leg {leg.leg_index} ({leg.option_type.value} {leg.strike}): {leg.error}"
                for leg in failed_legs
            )

    budget_fit = None
    candidate: StrategyCandidate | None = None
    if failure_reason is None:
        option_legs = [
            OptionLeg(
                option_type=leg.option_type,
                action=Action.BUY if leg.action == OptionAction.BUY else Action.SELL,
                strike=leg.strike,
                premium=leg.benchmark_entry_price,  # type: ignore[arg-type]
                quantity=leg.quantity,
            )
            for leg in leg_quotes
        ]
        analysis = analyze(option_legs)
        # decision_snapshot.legs and .strategy_type are always set
        # together (see services/decision_snapshot_freezing.py) -- legs
        # was already confirmed non-empty above, so strategy_type is
        # guaranteed non-None here too.
        assert decision_snapshot.strategy_type is not None
        candidate = StrategyCandidate(
            category=StrategyCategory(decision_snapshot.strategy_type),
            legs=tuple(option_legs),
            analysis=analysis,
            expiration=decision_snapshot.selected_expiration,  # type: ignore[arg-type]
            underlying_price=underlying_price,  # type: ignore[arg-type]
        )
        risk_cap = DEFAULT_MAX_RISK_UTILIZATION_PCT[portfolio.risk_profile]
        budget_fit = compute_budget_fit(
            candidate,
            trade_budget=portfolio.cash_balance,
            risk_cap=risk_cap,
            risk_cap_is_percent=True,
        )
        if not budget_fit.feasible or budget_fit.max_feasible_quantity < 1:
            failure_reason = (
                f"${portfolio.cash_balance} {portfolio.risk_profile.value.title()} budget "
                "cannot size even one contract of this structure"
            )
        elif budget_fit.remaining_budget is not None and budget_fit.remaining_budget < 0:
            failure_reason = "sizing produced a negative remaining budget -- refusing to enter"
        elif (
            budget_fit.budget_utilization_pct is not None
            and budget_fit.budget_utilization_pct > 100
        ):
            failure_reason = "sizing produced >100% capital utilization -- refusing to enter"

    status = CaptureStatus.FAILED if failure_reason is not None else CaptureStatus.CAPTURED

    attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision_snapshot.id,
        benchmark_portfolio_id=portfolio.id,
        status=status,
        capture_error=failure_reason,
        underlying_price=underlying_price,
        # No provider today returns a separate underlying bid/ask
        # alongside the option chain (see OptionQuote -- only a single
        # underlying_price/underlying_timestamp pair) -- left null
        # rather than fabricated, per Phase 4.4 sec 5/7's own rule.
        underlying_bid=None,
        underlying_ask=None,
        underlying_timestamp=underlying_timestamp,
        option_market_timestamp=option_market_timestamp,
        net_entry_price_per_share=(
            candidate.analysis.net_premium
            if status == CaptureStatus.CAPTURED and candidate is not None
            else None
        ),
        net_entry_cash=(budget_fit.total_net_premium if budget_fit is not None else None),
        contracts=(budget_fit.max_feasible_quantity if budget_fit is not None else None),
        initial_max_risk=(budget_fit.total_max_loss if budget_fit is not None else None),
        capital_utilization=(
            budget_fit.budget_utilization_pct if budget_fit is not None else None
        ),
        source_provider=quotes[0].source_provider if quotes else None,
        captured_at=now if status == CaptureStatus.CAPTURED else None,
    )
    db.add(attempt)
    db.flush()

    for leg in leg_quotes:
        quote = leg.quote
        db.add(
            EntrySnapshot(
                decision_id=decision_snapshot.id,
                capture_attempt_id=attempt.id,
                leg_index=leg.leg_index,
                status=CaptureStatus.CAPTURED if leg.error is None else CaptureStatus.FAILED,
                captured_at=now if leg.error is None else None,
                external_contract_id=quote.external_contract_id if quote else None,
                expiration=decision_snapshot.selected_expiration,
                strike=leg.strike,
                option_type=leg.option_type,
                action=leg.action,
                quantity=leg.quantity,
                multiplier=Decimal(100),
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
                benchmark_entry_price=leg.benchmark_entry_price,
                pricing_assumption=leg.pricing_assumption,
                capture_error=leg.error,
                source_provider=quote.source_provider if quote else None,
            )
        )
    db.flush()

    log.info(
        "benchmark entry capture: decision_snapshot_id=%s status=%s reason=%s",
        decision_snapshot.id,
        status.value,
        failure_reason,
    )
    return attempt
