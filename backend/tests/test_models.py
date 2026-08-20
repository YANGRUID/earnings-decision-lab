from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, ProgrammingError

from models.benchmark_portfolio import BenchmarkPortfolio
from models.company import Company
from models.decision_snapshot import DecisionSnapshot
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.entry_snapshot import EntrySnapshot
from models.enums import (
    AnnouncementTime,
    CaptureStatus,
    DecisionDirection,
    DecisionSnapshotStatus,
    OptionAction,
    OptionType,
)
from models.settlement_snapshot import SettlementSnapshot


def test_company_earnings_event_result_relationship(db_session):
    company = Company(ticker="TEST1", name="Test Company One", cik="9999999991")
    db_session.add(company)
    db_session.flush()

    event = EarningsEvent(
        company_id=company.id,
        fiscal_year=2025,
        fiscal_quarter=4,
        earnings_date=date(2025, 9, 23),
        announcement_time=AnnouncementTime.AFTER_MARKET,
        date_confirmed=True,
    )
    db_session.add(event)
    db_session.flush()

    result = EarningsResult(
        earnings_event_id=event.id,
        actual_eps=Decimal("2.98"),
        actual_revenue=Decimal("11320000000"),
        source_provider="sec_edgar_xbrl",
        retrieved_at=datetime.now(UTC),
    )
    db_session.add(result)
    db_session.flush()

    db_session.refresh(event)
    assert event.company.ticker == "TEST1"
    assert event.result.actual_eps == Decimal("2.98")


def test_earnings_event_unique_constraint(db_session):
    company = Company(ticker="TEST2", name="Test Company Two", cik="9999999992")
    db_session.add(company)
    db_session.flush()

    db_session.add(EarningsEvent(company_id=company.id, fiscal_year=2026, fiscal_quarter=2))
    db_session.flush()

    db_session.add(EarningsEvent(company_id=company.id, fiscal_year=2026, fiscal_quarter=2))
    with pytest.raises(IntegrityError):
        db_session.flush()


def _make_decision_snapshot(**overrides) -> DecisionSnapshot:
    defaults = dict(
        ticker="TESTP4",
        company_name="Phase 4 Test Co",
        strategy_direction=DecisionDirection.BULLISH,
        strategy_type="iron_condor",
        generated_at=datetime(2026, 8, 20, 15, 55, tzinfo=UTC),
        status=DecisionSnapshotStatus.PENDING_ENTRY,
    )
    defaults.update(overrides)
    return DecisionSnapshot(**defaults)


def test_decision_snapshot_can_exist_without_settlement(db_session):
    """A frozen decision is real on its own -- entry/settlement capture
    happen later (Phase 4.4/4.5), not built yet, and a decision with
    neither is a normal, valid state (PENDING_ENTRY)."""
    decision = _make_decision_snapshot()
    db_session.add(decision)
    db_session.flush()

    db_session.refresh(decision)
    assert decision.id is not None
    assert decision.status == DecisionSnapshotStatus.PENDING_ENTRY
    assert decision.entry_snapshots == []
    assert decision.settlement_snapshots == []


def test_entry_snapshot_links_correctly(db_session):
    """A multi-leg capture attempt: two EntrySnapshot rows share one
    decision_id (one per leg) and both traverse back to the same parent
    via the relationship, matching this table's per-leg grain."""
    decision = _make_decision_snapshot(ticker="TESTP4B")
    db_session.add(decision)
    db_session.flush()

    captured_at = datetime(2026, 8, 20, 15, 55, tzinfo=UTC)
    leg_buy = EntrySnapshot(
        decision_id=decision.id,
        status=CaptureStatus.CAPTURED,
        captured_at=captured_at,
        strike=Decimal("100.00"),
        option_type=OptionType.CALL,
        action=OptionAction.BUY,
        bid=Decimal("1.20"),
        ask=Decimal("1.30"),
        mid=Decimal("1.25"),
        source_provider="ibkr",
    )
    leg_sell = EntrySnapshot(
        decision_id=decision.id,
        status=CaptureStatus.CAPTURED,
        captured_at=captured_at,
        strike=Decimal("110.00"),
        option_type=OptionType.CALL,
        action=OptionAction.SELL,
        bid=Decimal("0.60"),
        ask=Decimal("0.70"),
        mid=Decimal("0.65"),
        source_provider="ibkr",
    )
    db_session.add_all([leg_buy, leg_sell])
    db_session.flush()

    db_session.refresh(decision)
    assert {leg.id for leg in decision.entry_snapshots} == {leg_buy.id, leg_sell.id}
    assert leg_buy.decision_snapshot.ticker == "TESTP4B"
    assert leg_sell.decision_snapshot.id == decision.id


def test_settlement_snapshot_cannot_exist_without_decision(db_session):
    """FK integrity: a settlement_snapshot pointing at a decision_id that
    was never persisted is rejected by the database, not silently
    accepted as an orphan row."""
    db_session.add(
        SettlementSnapshot(
            decision_id=999_999,
            status=CaptureStatus.FAILED,
            earnings_date=date(2026, 8, 21),
            capture_error="test: no such decision",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_invalid_decision_relationships_fail_for_both_child_tables(db_session):
    """Duplicate invalid relationships fail consistently: an EntrySnapshot
    pointing at a nonexistent decision_id is rejected the same way a
    SettlementSnapshot pointing at that same bogus id is (see the
    previous test) -- the FK guarantee isn't accidentally one-sided."""
    db_session.add(
        EntrySnapshot(
            decision_id=999_998,
            status=CaptureStatus.FAILED,
            capture_error="test: no such decision",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_decision_snapshot_timestamps_stored_correctly(db_session):
    """generated_at round-trips exactly as stored, and stays untouched by
    a later status transition -- the "immutable generated timestamp"
    requirement, verified as real behavior, not just a docstring claim."""
    generated_at = datetime(2026, 8, 20, 15, 55, 0, tzinfo=UTC)
    decision = _make_decision_snapshot(ticker="TESTP4C", generated_at=generated_at)
    db_session.add(decision)
    db_session.flush()
    db_session.refresh(decision)

    assert decision.generated_at == generated_at
    assert decision.created_at is not None

    decision.status = DecisionSnapshotStatus.ENTERED
    db_session.flush()
    db_session.refresh(decision)

    assert decision.status == DecisionSnapshotStatus.ENTERED
    assert decision.generated_at == generated_at


def test_entry_snapshot_rejects_update(db_session):
    """The BEFORE UPDATE trigger installed by migration 78ee400f83ab
    makes this a real, enforced DB guarantee, not just a service-layer
    convention -- a retry after a failed capture must INSERT a new row."""
    decision = _make_decision_snapshot(ticker="TESTP4D")
    db_session.add(decision)
    db_session.flush()

    entry = EntrySnapshot(
        decision_id=decision.id,
        status=CaptureStatus.FAILED,
        capture_error="test: simulated IBKR failure",
    )
    db_session.add(entry)
    db_session.flush()

    entry.status = CaptureStatus.CAPTURED
    with pytest.raises(ProgrammingError, match="insert-only"):
        db_session.flush()


def test_benchmark_portfolio_name_must_be_unique(db_session):
    db_session.add(
        BenchmarkPortfolio(
            name="Moderate $2000",
            initial_capital=Decimal("2000.00"),
            cash_balance=Decimal("2000.00"),
        )
    )
    db_session.flush()

    db_session.add(
        BenchmarkPortfolio(
            name="Moderate $2000",
            initial_capital=Decimal("2000.00"),
            cash_balance=Decimal("2000.00"),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
