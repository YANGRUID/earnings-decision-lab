from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, ProgrammingError

from models.benchmark_portfolio import BenchmarkPortfolio
from models.company import Company
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.entry_capture_attempt import EntryCaptureAttempt
from models.entry_snapshot import EntrySnapshot
from models.enums import (
    AnnouncementTime,
    CaptureStatus,
    DecisionDirection,
    DecisionSnapshotStatus,
    EarningsCalendarEventStatus,
    EarningsTiming,
    EntryPolicy,
    ExitPolicy,
    ExpirationMode,
    OptionAction,
    OptionType,
    RiskProfile,
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


def test_earnings_calendar_event_unique_symbol_date(db_session):
    db_session.add(
        EarningsCalendarEvent(
            symbol="TESTNVDA",
            company_name="Test NVIDIA Corp",
            earnings_date=date(2026, 11, 25),
            earnings_time=EarningsTiming.AMC,
        )
    )
    db_session.flush()

    db_session.add(
        EarningsCalendarEvent(
            symbol="TESTNVDA",
            company_name="Test NVIDIA Corp",
            earnings_date=date(2026, 11, 25),
            earnings_time=EarningsTiming.AMC,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_earnings_calendar_event_historical_events_preserved(db_session):
    """No delete pathway exists anywhere in this schema -- a historical
    (COMPLETED) row just sits there, untouched, once written."""
    historical = EarningsCalendarEvent(
        symbol="TESTMU",
        company_name="Test Micron Technology",
        earnings_date=date(2025, 9, 23),
        earnings_time=EarningsTiming.AMC,
        status=EarningsCalendarEventStatus.COMPLETED,
    )
    db_session.add(historical)
    db_session.flush()
    historical_id = historical.id

    db_session.expire_all()
    reloaded = db_session.get(EarningsCalendarEvent, historical_id)
    assert reloaded is not None
    assert reloaded.status == EarningsCalendarEventStatus.COMPLETED


def _seed_event_and_portfolio(
    db_session, symbol: str = "TESTP4"
) -> tuple[EarningsCalendarEvent, BenchmarkPortfolio]:
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="Phase 4 Test Co",
        earnings_date=date(2026, 8, 20),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"Test Portfolio {symbol}",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
    )
    db_session.add_all([event, portfolio])
    db_session.flush()
    return event, portfolio


def _make_decision_snapshot(**overrides) -> DecisionSnapshot:
    defaults = dict(
        ticker="TESTP4",
        company_name="Phase 4 Test Co",
        strategy_direction=DecisionDirection.BULLISH,
        strategy_type="iron_condor",
        generated_at=datetime(2026, 8, 20, 15, 55, tzinfo=UTC),
        status=DecisionSnapshotStatus.PENDING_ENTRY,
        engine_version="options-decision-engine-v3",
        prompt_version="v1",
        expiration_source="v3_auto_resolver",
    )
    defaults.update(overrides)
    return DecisionSnapshot(**defaults)


def test_decision_snapshot_can_exist_without_settlement(db_session):
    """A frozen decision is real on its own -- entry/settlement capture
    happen later (Phase 4.4/4.5), not built yet, and a decision with
    neither is a normal, valid state (PENDING_ENTRY)."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _make_decision_snapshot(
        earnings_calendar_event_id=event.id, benchmark_portfolio_id=portfolio.id
    )
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
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="TESTP4B")
    decision = _make_decision_snapshot(
        ticker="TESTP4B",
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
    )
    db_session.add(decision)
    db_session.flush()

    attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=CaptureStatus.CAPTURED,
    )
    db_session.add(attempt)
    db_session.flush()

    captured_at = datetime(2026, 8, 20, 15, 55, tzinfo=UTC)
    leg_buy = EntrySnapshot(
        decision_id=decision.id,
        capture_attempt_id=attempt.id,
        leg_index=0,
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
        capture_attempt_id=attempt.id,
        leg_index=1,
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
    """generated_at round-trips exactly as stored."""
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="TESTP4C")
    generated_at = datetime(2026, 8, 20, 15, 55, 0, tzinfo=UTC)
    decision = _make_decision_snapshot(
        ticker="TESTP4C",
        generated_at=generated_at,
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
    )
    db_session.add(decision)
    db_session.flush()
    db_session.refresh(decision)

    assert decision.generated_at == generated_at
    assert decision.created_at is not None


def test_decision_snapshot_rejects_update(db_session):
    """Phase 4.3 decision #5: no UPDATE of frozen decisions, full stop --
    unlike Phase 4.1's original design, not even ``status`` is exempt.
    The same reject_snapshot_update() trigger entry_snapshot already uses
    is installed on decision_snapshot too (migration 201cc8a16cb0)."""
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="TESTP4E")
    decision = _make_decision_snapshot(
        ticker="TESTP4E",
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
    )
    db_session.add(decision)
    db_session.flush()

    decision.status = DecisionSnapshotStatus.ENTERED
    with pytest.raises(ProgrammingError, match="insert-only"):
        db_session.flush()


def test_entry_snapshot_rejects_update(db_session):
    """The BEFORE UPDATE trigger installed by migration 78ee400f83ab
    makes this a real, enforced DB guarantee, not just a service-layer
    convention -- a retry after a failed capture must INSERT a new row."""
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="TESTP4D")
    decision = _make_decision_snapshot(
        ticker="TESTP4D",
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
    )
    db_session.add(decision)
    db_session.flush()

    attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=CaptureStatus.FAILED,
        capture_error="test: simulated IBKR failure",
    )
    db_session.add(attempt)
    db_session.flush()

    entry = EntrySnapshot(
        decision_id=decision.id,
        capture_attempt_id=attempt.id,
        leg_index=0,
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


def test_benchmark_portfolio_policy_defaults(db_session):
    """Phase 4.4 sec 0B/1: constructing a row without explicitly setting
    the policy fields still gets the official benchmark's real defaults
    -- $2,000 capital is asserted by the caller (no column default for a
    dollar figure), but risk_profile/expiration_mode/entry_policy/
    exit_policy/is_active all come from the model's own real defaults
    once flushed (SQLAlchemy column defaults resolve at flush time, not
    at plain Python construction)."""
    portfolio = BenchmarkPortfolio(
        name="Policy Defaults Test",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
    )
    db_session.add(portfolio)
    db_session.flush()
    assert portfolio.initial_capital == Decimal("2000.00")
    assert portfolio.risk_profile == RiskProfile.MODERATE
    assert portfolio.expiration_mode == ExpirationMode.AUTO
    assert portfolio.entry_policy == EntryPolicy.PRE_EARNINGS_15_55_ET
    assert portfolio.exit_policy == ExitPolicy.FIRST_POST_EARNINGS_TRADING_DAY_CLOSE
    assert portfolio.is_active is True


def test_official_ai_earnings_benchmark_seed_row_persisted(db_session):
    """The real, migrated seed row (641899980b94, renamed from Phase
    4.3's 'Default Benchmark Portfolio') -- not a fixture, the actual
    official benchmark this whole phase's capture logic reads."""
    official = (
        db_session.query(BenchmarkPortfolio).filter_by(name="AI Earnings Benchmark").one()
    )
    assert official.initial_capital == Decimal("2000.00")
    assert official.risk_profile == RiskProfile.MODERATE
    assert official.expiration_mode == ExpirationMode.AUTO
    assert official.entry_policy == EntryPolicy.PRE_EARNINGS_15_55_ET
    assert official.exit_policy == ExitPolicy.FIRST_POST_EARNINGS_TRADING_DAY_CLOSE
    assert official.is_active is True


def _seed_event_portfolio_decision_for_attempt(db_session, symbol: str = "TESTEC44"):
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="Entry Capture Attempt Test Co",
        earnings_date=date(2026, 9, 16),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"EC Attempt Test Portfolio {symbol}",
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
    return event, portfolio, decision


def test_entry_capture_attempt_rejects_a_second_captured_attempt(db_session):
    """The real, DB-level idempotency guarantee (Phase 4.4 sec 15): a
    partial unique index on (decision_snapshot_id, benchmark_portfolio_id)
    WHERE status='CAPTURED' rejects a second successful attempt outright
    -- not just a service-layer convention."""
    _event, portfolio, decision = _seed_event_portfolio_decision_for_attempt(db_session)

    db_session.add(
        EntryCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.CAPTURED,
        )
    )
    db_session.flush()

    db_session.add(
        EntryCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.CAPTURED,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_entry_capture_attempt_allows_multiple_failed_attempts(db_session):
    """FAILED/PENDING rows are unrestricted -- only CAPTURED is unique --
    so retries after a real failure are never blocked at the DB level."""
    _event, portfolio, decision = _seed_event_portfolio_decision_for_attempt(
        db_session, symbol="TESTEC44B"
    )

    db_session.add(
        EntryCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error="first failure",
        )
    )
    db_session.add(
        EntryCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error="second failure",
        )
    )
    db_session.flush()

    count = (
        db_session.query(EntryCaptureAttempt)
        .filter_by(decision_snapshot_id=decision.id, benchmark_portfolio_id=portfolio.id)
        .count()
    )
    assert count == 2


def test_entry_capture_attempt_rejects_update(db_session):
    _event, portfolio, decision = _seed_event_portfolio_decision_for_attempt(
        db_session, symbol="TESTEC44C"
    )
    attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=CaptureStatus.FAILED,
        capture_error="test failure",
    )
    db_session.add(attempt)
    db_session.flush()

    attempt.status = CaptureStatus.CAPTURED
    with pytest.raises(ProgrammingError, match="insert-only"):
        db_session.flush()
