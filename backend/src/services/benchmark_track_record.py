"""Phase 4.6 -- AI Earnings Analyst Track Record: read-only aggregation
over the real, immutable Phase 4 forward-test tables (DecisionSnapshot +
EntrySnapshot + EntryCaptureAttempt + SettlementCaptureAttempt +
ExitSnapshot, plus BenchmarkPortfolio and VolatilitySnapshot for the two
real joins this needs). Never the legacy AIDecisionVersion track record
(services/track_record.py) -- explicit instruction, and a genuinely
different system: that one grades V3's manually-triggered single-ticker
journal and explicitly cannot compute a real win rate for lack of
point-in-time entry/exit prices; this module can, for the first time in
this project, because Phase 4.4/4.5 built exactly that missing data.

Pure read-side computation: no migration, no new table, no cache row
ever written -- see PHASE4_6_TRACK_RECORD_ARCHITECTURE_REVIEW.md for the
full design and the 2026-08-21 addendum for the approved decisions this
module implements (equity-curve-based Max Drawdown from real dollars,
never R-multiples; DTE measured from the earnings date, not decision
generation; five probability-calibration buckets including <60%).

Every rate here is a Rate(correct, total) whose .pct is None when
total == 0 -- see analytics/decision/track_record_math.py. This module
never substitutes 0 for "no sample," and never returns a fabricated
metric when settled_decisions == 0.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.decision.track_record_math import (
    PROBABILITY_BUCKETS,
    Rate,
    compute_average,
    compute_max_drawdown,
    compute_median,
    compute_profit_factor,
    dte_bucket_label,
    probability_bucket_label,
    rate_from_bools,
)
from analytics.options.payoff import Action, OptionLeg, analyze
from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.entry_capture_attempt import EntryCaptureAttempt
from models.entry_snapshot import EntrySnapshot
from models.enums import CaptureStatus, DecisionDirection, OptionAction, RiskProfile
from models.settlement_capture_attempt import SettlementCaptureAttempt
from models.volatility_snapshot import VolatilitySnapshot

log = logging.getLogger("services.benchmark_track_record")

_DIRECTION_SIGN: dict[DecisionDirection, int] = {
    DecisionDirection.STRONG_BULLISH: 1,
    DecisionDirection.BULLISH: 1,
    DecisionDirection.NEUTRAL: 0,
    DecisionDirection.BEARISH: -1,
    DecisionDirection.STRONG_BEARISH: -1,
}


@dataclass(frozen=True)
class TrackRecordFilters:
    """Every field independently optional and combinable -- a plain AND
    of whichever are present (PHASE4_6_TRACK_RECORD_ARCHITECTURE_
    REVIEW.md sec 5)."""

    strategy: str | None = None
    confidence_bucket: str | None = None  # one of PROBABILITY_BUCKETS' labels
    dte_bucket: str | None = None  # one of DTE_BUCKETS' labels
    risk_profile: RiskProfile | None = None
    iv_regime: str | None = None


@dataclass(frozen=True)
class BenchmarkTrackRecordSummary:
    portfolio_id: int
    total_decisions: int
    settled_decisions: int
    win_rate: Rate
    average_r: Decimal | None
    median_r: Decimal | None
    expectancy: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal | None
    max_drawdown_pct: Decimal | None
    directional_accuracy: Rate
    breakeven_accuracy: Rate
    range_accuracy: Rate


@dataclass(frozen=True)
class CalibrationBucket:
    label: str
    lower: int | None
    upper: int | None
    rate: Rate


@dataclass(frozen=True)
class BenchmarkCalibrationSummary:
    portfolio_id: int
    settled_decisions: int
    buckets: list[CalibrationBucket]


def resolve_portfolio(db: Session, portfolio_id: int | None) -> BenchmarkPortfolio | None:
    """The single active benchmark portfolio by default -- the same
    resolution services/scheduler.py's jobs already use -- or a specific
    one when ``portfolio_id`` is given (multi-portfolio-ready, even
    though today there is exactly one)."""
    if portfolio_id is not None:
        return db.get(BenchmarkPortfolio, portfolio_id)
    return (
        db.query(BenchmarkPortfolio)
        .filter_by(is_active=True)
        .order_by(BenchmarkPortfolio.id)
        .first()
    )


def _dte_for_decision(decision: DecisionSnapshot) -> int | None:
    """selected_expiration - earnings_calendar_event.earnings_date
    (Phase 4.6 approved decision 3 -- days from the earnings event to
    expiration, not from decision generation). None when no expiration
    was ever selected -- a real, honest "no recommended strategy"
    outcome, never guessed."""
    if decision.selected_expiration is None:
        return None
    return (decision.selected_expiration - decision.earnings_calendar_event.earnings_date).days


def _matches_filters(
    decision: DecisionSnapshot, portfolio: BenchmarkPortfolio, filters: TrackRecordFilters
) -> bool:
    if filters.strategy is not None and decision.strategy_type != filters.strategy:
        return False
    if filters.risk_profile is not None and portfolio.risk_profile != filters.risk_profile:
        # risk_profile is a portfolio-level attribute, not a per-decision
        # one (see the architecture review sec 2) -- today there is
        # exactly one portfolio, so this either matches every decision
        # in the query or none of them; kept as a real, correct filter
        # for whenever a second portfolio exists.
        return False
    if filters.iv_regime is not None and decision.volatility_regime != filters.iv_regime:
        return False
    if filters.confidence_bucket is not None:
        if decision.estimated_probability is None:
            return False
        if probability_bucket_label(decision.estimated_probability) != filters.confidence_bucket:
            return False
    if filters.dte_bucket is not None:
        dte = _dte_for_decision(decision)
        if dte is None or dte_bucket_label(dte) != filters.dte_bucket:
            return False
    return True


def _fetch_filtered_decisions(
    db: Session, portfolio: BenchmarkPortfolio, filters: TrackRecordFilters
) -> list[DecisionSnapshot]:
    decisions = (
        db.query(DecisionSnapshot).filter(DecisionSnapshot.benchmark_portfolio_id == portfolio.id)
    ).all()
    return [d for d in decisions if _matches_filters(d, portfolio, filters)]


def _direction_correct(
    strategy_direction: DecisionDirection, entry_price: Decimal, exit_price: Decimal
) -> bool | None:
    """None for a neutral view (nothing to grade) or a zero entry price
    (can't determine a real move) -- never guessed."""
    sign = _DIRECTION_SIGN[strategy_direction]
    if sign == 0 or entry_price == 0:
        return None
    if exit_price == entry_price:
        return False  # no real move at all can't match a directional call
    actual_sign = 1 if exit_price > entry_price else -1
    return actual_sign == sign


def _breakeven_correct(
    entry_legs: list[EntrySnapshot],
    entry_underlying_price: Decimal,
    exit_underlying_price: Decimal,
) -> bool | None:
    """Mirrors services/decision_settlement.py::_breakeven_met's exact
    debit/credit logic -- reused as a pattern, not imported (that
    function is bound to AIDecisionVersion) -- recomputed from real
    Phase 4 entry legs via the same analytics/options/payoff.py::
    analyze() entry capture itself already calls, never a second,
    parallel breakeven-math implementation. Unlike V3's own derived
    ``actual_price = underlying_price * (1 + actual_move_pct)``
    approximation, this compares against the real, captured exit
    underlying price directly."""
    usable_legs = [
        leg
        for leg in entry_legs
        if leg.option_type is not None
        and leg.action is not None
        and leg.strike is not None
        and leg.benchmark_entry_price is not None
    ]
    if not usable_legs:
        return None
    option_legs = [
        OptionLeg(
            option_type=leg.option_type,  # type: ignore[arg-type]
            action=Action.BUY if leg.action == OptionAction.BUY else Action.SELL,
            strike=leg.strike,  # type: ignore[arg-type]
            premium=leg.benchmark_entry_price,  # type: ignore[arg-type]
            quantity=leg.quantity or 1,
        )
        for leg in usable_legs
    ]
    analysis = analyze(option_legs)
    if not analysis.breakevens:
        return None
    nearest = min(analysis.breakevens, key=lambda be: abs(be - entry_underlying_price))
    # A debit position needs to clear its breakeven; a credit position
    # profits by staying within it.
    requires_move_beyond = analysis.net_premium >= 0
    if requires_move_beyond:
        if nearest >= entry_underlying_price:
            return exit_underlying_price >= nearest
        return exit_underlying_price <= nearest
    # A credit position profits by staying within its breakeven.
    if nearest >= entry_underlying_price:
        return exit_underlying_price < nearest
    return exit_underlying_price > nearest


def _range_correct(
    implied_move_pct: Decimal, entry_underlying_price: Decimal, exit_underlying_price: Decimal
) -> bool | None:
    """Did the real outcome stay inside the option market's own implied
    range at decision time -- a market-calibration question, distinct
    from directional accuracy (sign, not magnitude) and breakeven
    accuracy (the strategy's own breakeven, not the market's implied
    move). None for a zero entry price (can't determine a real move)."""
    if entry_underlying_price == 0:
        return None
    actual_move_pct = abs(
        (exit_underlying_price - entry_underlying_price) / entry_underlying_price
    )
    return actual_move_pct <= implied_move_pct


@dataclass
class _SettledFacts:
    decision_id: int
    estimated_probability: Decimal | None
    realized_pnl: Decimal
    r_multiple: Decimal | None
    is_win: bool
    captured_at: object  # datetime, kept loosely typed here -- only ever sorted, never parsed
    directional_correct: bool | None
    breakeven_correct: bool | None
    range_correct: bool | None


def _collect_settled_facts(
    db: Session, decisions: list[DecisionSnapshot], portfolio: BenchmarkPortfolio
) -> list[_SettledFacts]:
    if not decisions:
        return []
    decision_ids = [d.id for d in decisions]

    settlements = {
        row.decision_snapshot_id: row
        for row in db.query(SettlementCaptureAttempt).filter(
            SettlementCaptureAttempt.decision_snapshot_id.in_(decision_ids),
            SettlementCaptureAttempt.benchmark_portfolio_id == portfolio.id,
            SettlementCaptureAttempt.status == CaptureStatus.CAPTURED,
        )
    }
    if not settlements:
        return []

    entry_attempt_ids = [
        s.entry_capture_attempt_id for s in settlements.values() if s.entry_capture_attempt_id
    ]
    entry_attempts = {
        row.id: row
        for row in db.query(EntryCaptureAttempt).filter(
            EntryCaptureAttempt.id.in_(entry_attempt_ids)
        )
    }
    entry_legs_by_attempt: dict[int, list[EntrySnapshot]] = {}
    for row in db.query(EntrySnapshot).filter(
        EntrySnapshot.capture_attempt_id.in_(entry_attempt_ids)
    ):
        entry_legs_by_attempt.setdefault(row.capture_attempt_id, []).append(row)

    volatility_snapshot_ids = [
        d.option_snapshot_reference for d in decisions if d.option_snapshot_reference is not None
    ]
    volatility_snapshots = {
        row.id: row
        for row in db.query(VolatilitySnapshot).filter(
            VolatilitySnapshot.id.in_(volatility_snapshot_ids)
        )
    }

    facts: list[_SettledFacts] = []
    for decision in decisions:
        settlement = settlements.get(decision.id)
        if settlement is None:
            continue
        entry_attempt = (
            entry_attempts.get(settlement.entry_capture_attempt_id)
            if settlement.entry_capture_attempt_id
            else None
        )

        directional_correct = None
        breakeven_correct = None
        range_correct = None
        if (
            entry_attempt is not None
            and entry_attempt.underlying_price is not None
            and settlement.underlying_price is not None
        ):
            directional_correct = _direction_correct(
                decision.strategy_direction,
                entry_attempt.underlying_price,
                settlement.underlying_price,
            )
            entry_legs = entry_legs_by_attempt.get(entry_attempt.id, [])
            breakeven_correct = _breakeven_correct(
                entry_legs, entry_attempt.underlying_price, settlement.underlying_price
            )
            volatility_snapshot = (
                volatility_snapshots.get(decision.option_snapshot_reference)
                if decision.option_snapshot_reference is not None
                else None
            )
            if volatility_snapshot is not None and volatility_snapshot.implied_move_pct is not None:
                range_correct = _range_correct(
                    volatility_snapshot.implied_move_pct,
                    entry_attempt.underlying_price,
                    settlement.underlying_price,
                )

        facts.append(
            _SettledFacts(
                decision_id=decision.id,
                estimated_probability=decision.estimated_probability,
                realized_pnl=settlement.realized_pnl or Decimal(0),
                r_multiple=settlement.r_multiple,
                is_win=bool(settlement.is_win),
                captured_at=settlement.captured_at,
                directional_correct=directional_correct,
                breakeven_correct=breakeven_correct,
                range_correct=range_correct,
            )
        )
    return facts


def compute_benchmark_track_record(
    db: Session, portfolio: BenchmarkPortfolio, filters: TrackRecordFilters
) -> BenchmarkTrackRecordSummary:
    decisions = _fetch_filtered_decisions(db, portfolio, filters)
    facts = _collect_settled_facts(db, decisions, portfolio)
    facts.sort(key=lambda f: f.captured_at)  # type: ignore[arg-type,return-value]

    r_multiples = [f.r_multiple for f in facts if f.r_multiple is not None]
    realized_pnls = [f.realized_pnl for f in facts]
    drawdown = compute_max_drawdown(portfolio.initial_capital, realized_pnls)

    return BenchmarkTrackRecordSummary(
        portfolio_id=portfolio.id,
        total_decisions=len(decisions),
        settled_decisions=len(facts),
        win_rate=rate_from_bools([f.is_win for f in facts]),
        average_r=compute_average(r_multiples),
        median_r=compute_median(r_multiples),
        expectancy=compute_average(r_multiples),
        profit_factor=compute_profit_factor(realized_pnls),
        max_drawdown=drawdown.max_drawdown,
        max_drawdown_pct=drawdown.max_drawdown_pct,
        directional_accuracy=rate_from_bools(
            [f.directional_correct for f in facts if f.directional_correct is not None]
        ),
        breakeven_accuracy=rate_from_bools(
            [f.breakeven_correct for f in facts if f.breakeven_correct is not None]
        ),
        range_accuracy=rate_from_bools(
            [f.range_correct for f in facts if f.range_correct is not None]
        ),
    )


def compute_benchmark_calibration(
    db: Session, portfolio: BenchmarkPortfolio
) -> BenchmarkCalibrationSummary:
    """Unfiltered by strategy/DTE/etc. -- calibration is a property of
    the AI's probability estimates against real outcomes across the
    whole portfolio, not a per-slice breakdown (the slicing filters in
    TrackRecordFilters apply to /track-record, not /calibration -- see
    the architecture review sec 6)."""
    decisions = _fetch_filtered_decisions(db, portfolio, TrackRecordFilters())
    facts = _collect_settled_facts(db, decisions, portfolio)
    # settled_decisions counts every real settlement (matching /track-
    # record's own definition -- "0 settled trades" must mean the same
    # thing on both endpoints), even though a bucket's own rate.total
    # only ever reflects the subset that also had a gradable probability
    # (estimated_probability is nullable -- see DecisionSnapshot).
    graded = [
        (f.estimated_probability, f.is_win) for f in facts if f.estimated_probability is not None
    ]

    buckets = [
        CalibrationBucket(
            label=label,
            lower=lower,
            upper=upper,
            rate=rate_from_bools(
                [
                    is_win
                    for probability, is_win in graded
                    if probability_bucket_label(probability) == label
                ]
            ),
        )
        for label, lower, upper in PROBABILITY_BUCKETS
    ]

    return BenchmarkCalibrationSummary(
        portfolio_id=portfolio.id, settled_decisions=len(facts), buckets=buckets
    )
