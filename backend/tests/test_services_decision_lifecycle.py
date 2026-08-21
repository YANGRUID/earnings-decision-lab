"""Phase 4.3/4.4/4.5 -- tests for services/decision_lifecycle.py, the
pure read-side derivation of PENDING_ENTRY/ENTERED/SETTLED. Had no
dedicated test file before Phase 4.5 (only exercised indirectly through
the capture services) -- added now since has_settlement changed to
query settlement_capture_attempt instead of the old settlement_snapshot,
a real, meaningful behavior change worth its own direct coverage."""

from datetime import UTC, date, datetime
from decimal import Decimal

from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.enums import CaptureStatus, DecisionDirection, DecisionSnapshotStatus, EarningsTiming
from models.settlement_capture_attempt import SettlementCaptureAttempt
from services.decision_lifecycle import (
    DecisionLifecycleStage,
    decision_lifecycle_stage,
    has_official_entry,
    has_settlement,
)


def _seed_decision(db_session, symbol: str = "TESTLC45"):
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="Lifecycle Test Co",
        earnings_date=date(2026, 9, 16),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"Lifecycle Test Portfolio {symbol}",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
    )
    db_session.add_all([event, portfolio])
    db_session.flush()
    decision = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=symbol,
        company_name=event.company_name,
        strategy_direction=DecisionDirection.BULLISH,
        strategy_type="long_call",
        generated_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        status=DecisionSnapshotStatus.PENDING_ENTRY,
        engine_version="options-decision-engine-v3",
        prompt_version="v1",
        expiration_source="v3_auto_resolver",
    )
    db_session.add(decision)
    db_session.flush()
    return portfolio, decision


def test_pending_entry_when_nothing_captured_yet(db_session):
    portfolio, decision = _seed_decision(db_session)

    assert has_official_entry(db_session, decision.id, portfolio.id) is False
    assert has_settlement(db_session, decision.id, portfolio.id) is False
    assert (
        decision_lifecycle_stage(db_session, decision.id, portfolio.id)
        == DecisionLifecycleStage.PENDING_ENTRY
    )


def test_pending_entry_when_only_a_failed_entry_attempt_exists(db_session):
    portfolio, decision = _seed_decision(db_session, symbol="TESTLC45B")
    db_session.add(
        EntryCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error="test failure",
        )
    )
    db_session.flush()

    assert has_official_entry(db_session, decision.id, portfolio.id) is False
    assert (
        decision_lifecycle_stage(db_session, decision.id, portfolio.id)
        == DecisionLifecycleStage.PENDING_ENTRY
    )


def test_entered_when_a_captured_entry_attempt_exists(db_session):
    portfolio, decision = _seed_decision(db_session, symbol="TESTLC45C")
    db_session.add(
        EntryCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.CAPTURED,
        )
    )
    db_session.flush()

    assert has_official_entry(db_session, decision.id, portfolio.id) is True
    assert has_settlement(db_session, decision.id, portfolio.id) is False
    assert (
        decision_lifecycle_stage(db_session, decision.id, portfolio.id)
        == DecisionLifecycleStage.ENTERED
    )


def test_entered_not_settled_when_only_a_failed_settlement_exists(db_session):
    portfolio, decision = _seed_decision(db_session, symbol="TESTLC45D")
    entry_attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=CaptureStatus.CAPTURED,
    )
    db_session.add(entry_attempt)
    db_session.flush()
    db_session.add(
        SettlementCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            entry_capture_attempt_id=entry_attempt.id,
            status=CaptureStatus.FAILED,
            capture_error="test failure",
        )
    )
    db_session.flush()

    assert has_settlement(db_session, decision.id, portfolio.id) is False
    assert (
        decision_lifecycle_stage(db_session, decision.id, portfolio.id)
        == DecisionLifecycleStage.ENTERED
    )


def test_settled_when_a_captured_settlement_attempt_exists(db_session):
    portfolio, decision = _seed_decision(db_session, symbol="TESTLC45E")
    entry_attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=CaptureStatus.CAPTURED,
    )
    db_session.add(entry_attempt)
    db_session.flush()
    db_session.add(
        SettlementCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            entry_capture_attempt_id=entry_attempt.id,
            status=CaptureStatus.CAPTURED,
            realized_pnl=Decimal("100.00"),
        )
    )
    db_session.flush()

    assert has_settlement(db_session, decision.id, portfolio.id) is True
    assert (
        decision_lifecycle_stage(db_session, decision.id, portfolio.id)
        == DecisionLifecycleStage.SETTLED
    )


def test_lifecycle_is_scoped_per_portfolio(db_session):
    """A CAPTURED entry/settlement for a *different* portfolio must never
    make an unrelated portfolio's view of the same decision look entered
    or settled -- mirrors has_official_entry's own existing portfolio
    scoping, now also enforced for has_settlement (Phase 4.5 closes this
    pre-existing asymmetry)."""
    portfolio, decision = _seed_decision(db_session, symbol="TESTLC45F")
    other_portfolio = BenchmarkPortfolio(
        name="Other Portfolio TESTLC45F",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
    )
    db_session.add(other_portfolio)
    db_session.flush()

    entry_attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=CaptureStatus.CAPTURED,
    )
    db_session.add(entry_attempt)
    db_session.flush()
    db_session.add(
        SettlementCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            entry_capture_attempt_id=entry_attempt.id,
            status=CaptureStatus.CAPTURED,
        )
    )
    db_session.flush()

    assert has_official_entry(db_session, decision.id, other_portfolio.id) is False
    assert has_settlement(db_session, decision.id, other_portfolio.id) is False
    assert (
        decision_lifecycle_stage(db_session, decision.id, other_portfolio.id)
        == DecisionLifecycleStage.PENDING_ENTRY
    )
