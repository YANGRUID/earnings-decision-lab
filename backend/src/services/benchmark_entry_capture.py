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
second, parallel options-math system), services/options_
reconstruction.py's MAX_UNDERLYING_OPTION_SKEW constant for timestamp-
coherence validation, and analytics/earnings_timing.py's
compute_entry_exit_schedule() for the no-lookahead timing gate (Phase
4.4 sec 2/3) -- the exact same function services/decision_pipeline.py
already uses for the decision-generation timing gate, not a second
implementation of the same trading-day math.

Hardening pass (post-Phase-4.4): the official entry's underlying context
is always a live quote fetched via the provider's own
get_underlying_quote() (see providers/base.py, providers/ibkr_options.py)
-- never services/options_analytics.py's _latest_close_price_on_or_before()
daily-bar fallback, which V3's own research code still uses correctly
elsewhere but which this module must not fall back to: combining a live
option quote with a previous session's underlying close and calling it
one official entry would misrepresent what the benchmark could actually
have entered. A missing or stale (see MAX_UNDERLYING_OPTION_SKEW) live
underlying observation makes the official capture fail honestly instead.
The capture window itself is also validated on both sides now -- not just
"not too late" (LATE_CUTOFF_GRACE) but also "not materially early" (see
EARLY_CAPTURE_TOLERANCE below) -- a 10:00 ET capture must never stand in
for a 15:55 ET benchmark entry.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from providers.types import OptionQuote, UnderlyingQuote
from services.decision_pipeline import LATE_CUTOFF_GRACE
from services.options_reconstruction import MAX_UNDERLYING_OPTION_SKEW

log = logging.getLogger("services.benchmark_entry_capture")

_TIMING_TO_ANNOUNCEMENT_TIME: dict[EarningsTiming, AnnouncementTime] = {
    EarningsTiming.BMO: AnnouncementTime.BEFORE_MARKET,
    EarningsTiming.AMC: AnnouncementTime.AFTER_MARKET,
    EarningsTiming.DMH: AnnouncementTime.UNKNOWN,
    EarningsTiming.UNKNOWN: AnnouncementTime.UNKNOWN,
}

# Phase 4.4 hardening sec 2: the official capture window is symmetric
# around the scheduled entry timestamp, not just a late-side grace period
# -- narrow and deterministic enough that a materially early capture (e.g.
# 10:00 ET for a 15:55 ET benchmark) is rejected, not silently accepted as
# if it were the same market moment. Same 5-minute magnitude as
# LATE_CUTOFF_GRACE, kept as its own named constant since the two bound
# conceptually different risks (a late capture risks reacting to news that
# already broke; an early capture risks representing a materially
# different, stale market) and may need to diverge in the future.
EARLY_CAPTURE_TOLERANCE = timedelta(minutes=5)


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
    one at generation time.

    Hardening pass sec 2: the capture-time check is a real window on both
    sides of the scheduled entry timestamp (EARLY_CAPTURE_TOLERANCE ..
    LATE_CUTOFF_GRACE), not just a late-side cutoff -- a materially early
    capture (e.g. mid-morning for a 15:55 ET benchmark) does not represent
    the same market moment and must be rejected exactly like a late one.
    """
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
    if now < schedule.entry_timestamp - EARLY_CAPTURE_TOLERANCE:
        return False, (
            f"capture time ({now.isoformat()}) is before the valid pre-earnings entry window "
            f"({schedule.entry_timestamp.isoformat()} - {EARLY_CAPTURE_TOLERANCE}) -- refusing "
            "to capture a benchmark entry materially earlier than the scheduled entry, which "
            "would not represent the same market moment"
        )
    if now > schedule.entry_timestamp + LATE_CUTOFF_GRACE:
        return False, (
            f"capture time ({now.isoformat()}) is past the valid pre-earnings entry window "
            f"({schedule.entry_timestamp.isoformat()} + {LATE_CUTOFF_GRACE}) -- refusing to "
            "silently backfill an entry after the market may have already reacted"
        )
    return True, None


def _resolve_underlying(
    underlying: UnderlyingQuote | None, option_market_timestamp: datetime
) -> tuple[Decimal | None, Decimal | None, Decimal | None, datetime | None, str | None]:
    """Real underlying price/bid/ask/timestamp for this capture, validated
    for coherence against the option market's own timestamp, or an honest
    reason why the official capture cannot proceed.

    Hardening pass sec 1: ``underlying`` must be a live quote fetched via
    the provider's own get_underlying_quote() (see providers/base.py) --
    never a previous-session daily close. A missing quote (provider has no
    live underlying capability, or a real fetch failure) or excessive
    underlying/option timestamp skew both fail honestly here; neither ever
    falls back to services/options_analytics.py's
    _latest_close_price_on_or_before(), which remains correct and
    untouched for V3's own research/reconstruction code but must never
    let an OFFICIAL capture count as entered."""
    if underlying is None:
        reason = "no live underlying quote available from the options provider"
        return None, None, None, None, reason

    skew = abs(option_market_timestamp - underlying.timestamp)
    if skew > MAX_UNDERLYING_OPTION_SKEW:
        return None, None, None, None, (
            f"underlying/option quote timestamp skew ({skew}) exceeds "
            f"{MAX_UNDERLYING_OPTION_SKEW} -- refusing to combine a stale underlying "
            "observation with a fresh option quote in one official entry"
        )
    return underlying.price, underlying.bid, underlying.ask, underlying.timestamp, None


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
        # Fetched in the same try/except as the option chain: a real
        # provider failure fetching either one must produce the same
        # honest FAILED attempt, not a partial capture (Phase 4.4
        # hardening sec 1 -- see providers.base.OptionsDataProvider.
        # get_underlying_quote's own docstring for why this is never a
        # daily-close fallback instead).
        underlying_quote = options_provider.get_underlying_quote(decision_snapshot.ticker)
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

    option_market_timestamp = quotes[0].snapshot_timestamp if quotes else now

    underlying_price, underlying_bid, underlying_ask, underlying_timestamp, underlying_error = (
        _resolve_underlying(underlying_quote, option_market_timestamp)
    )

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
        # Real bid/ask from the provider's own live UnderlyingQuote when
        # it exposes them (IBKR does, see providers/ibkr_options.py); left
        # null, never fabricated, for a provider/quote that only offers a
        # last/market price (Phase 4.4 sec 5/7's own rule).
        underlying_bid=underlying_bid,
        underlying_ask=underlying_ask,
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
