"""Phase 4 forward-test evaluation dataset (2026-08-26), Sections 32-33.
No invented data: every assertion here traces to a real, explicitly-
seeded value."""

from datetime import UTC, date, datetime
from decimal import Decimal

from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.entry_snapshot import EntrySnapshot
from models.enums import (
    CaptureStatus,
    DecisionDirection,
    DecisionVolatilityView,
    EarningsTiming,
    OptionAction,
    OptionType,
    RiskProfile,
)
from models.settlement_capture_attempt import SettlementCaptureAttempt
from services.forward_test_dataset import build_forward_test_dataset_row, list_forward_test_dataset

EXP = date(2026, 9, 18)


def _seed_decision(db_session, ticker: str, **overrides) -> DecisionSnapshot:
    event = EarningsCalendarEvent(
        symbol=ticker,
        company_name="ZZ FTD Co",
        earnings_date=date(2026, 9, 17),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"{ticker} Portfolio", initial_capital=Decimal("2000"), cash_balance=Decimal("2000")
    )
    db_session.add_all([event, portfolio])
    db_session.flush()
    defaults = dict(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=ticker,
        company_name="ZZ FTD Co",
        strategy_direction=DecisionDirection.BULLISH,
        strategy_type="long_call",
        selected_expiration=EXP,
        legs=[
            {"option_type": "call", "action": "buy", "strike": "100", "premium": "2", "quantity": 1}
        ],
        volatility_view=DecisionVolatilityView.LONG_VOL,
        effective_risk_profile=RiskProfile.MODERATE,
        deterministic_confidence_score=70,
        score_breakdown={"direction_fit": 12},
        strategy_score=75,
        generated_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        engine_version="test",
        prompt_version="test",
        expiration_source="test",
    )
    defaults.update(overrides)
    decision = DecisionSnapshot(**defaults)
    db_session.add(decision)
    db_session.flush()
    return decision


def test_pre_event_fields_present_with_no_entry_or_settlement(db_session):
    decision = _seed_decision(db_session, "ZZFTD1")

    row = build_forward_test_dataset_row(decision, None, None)

    assert row.decision_snapshot_id == decision.id
    assert row.direction == "bullish"
    assert row.volatility_view == "long_vol"
    assert row.effective_risk_profile == "moderate"
    assert row.deterministic_confidence_score == 70
    assert row.strategy_score == 75
    assert row.dte_at_generation == 2  # Sept 18 - Sept 16
    assert row.entry_status is None
    assert row.settlement_status is None
    assert row.directional_correctness is None  # no real move known yet
    assert row.breakeven_held is None


def test_entry_fields_populated_when_captured(db_session):
    decision = _seed_decision(db_session, "ZZFTD2")
    entry = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=decision.benchmark_portfolio_id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("100"),
        net_entry_price_per_share=Decimal("2.10"),
        initial_max_risk=Decimal("210"),
        captured_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
    )
    db_session.add(entry)
    db_session.flush()
    db_session.add(
        EntrySnapshot(
            decision_id=decision.id,
            capture_attempt_id=entry.id,
            leg_index=0,
            status=CaptureStatus.CAPTURED,
            expiration=EXP,
            strike=Decimal("100"),
            option_type=OptionType.CALL,
            action=OptionAction.BUY,
            quantity=1,
            multiplier=Decimal(100),
            bid=Decimal("2.00"),
            ask=Decimal("2.10"),
            benchmark_entry_price=Decimal("2.10"),
            pricing_assumption="BUY_TO_OPEN_AT_ASK",
            delta=Decimal("0.5"),
        )
    )
    db_session.flush()
    db_session.refresh(entry)

    row = build_forward_test_dataset_row(decision, entry, None)

    assert row.entry_status == "captured"
    assert row.entry_underlying_price == Decimal("100")
    assert row.entry_capital_at_risk == Decimal("210")
    assert row.entry_legs is not None
    assert len(row.entry_legs) == 1
    assert row.entry_legs[0].pricing_assumption == "BUY_TO_OPEN_AT_ASK"
    assert row.entry_legs[0].delta == Decimal("0.5")


def test_settlement_fields_and_derived_metrics_when_settled(db_session):
    decision = _seed_decision(db_session, "ZZFTD3")
    entry = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=decision.benchmark_portfolio_id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("100"),
        net_entry_price_per_share=Decimal("2.10"),
        initial_max_risk=Decimal("210"),
    )
    db_session.add(entry)
    db_session.flush()
    settlement = SettlementCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=decision.benchmark_portfolio_id,
        entry_capture_attempt_id=entry.id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("115"),  # real underlying rose 15%
        realized_pnl=Decimal("80"),
        return_pct=Decimal("0.38"),
        r_multiple=Decimal("0.38"),
        is_win=True,
    )
    db_session.add(settlement)
    db_session.flush()
    db_session.refresh(settlement)

    row = build_forward_test_dataset_row(decision, entry, settlement)

    assert row.settlement_status == "captured"
    assert row.exit_underlying_price == Decimal("115")
    assert row.realized_pnl == Decimal("80")
    assert row.r_multiple == Decimal("0.38")
    assert row.is_win is True
    # Real, direct derivation: (115 - 100) / 100 = 0.15
    assert row.underlying_move_pct == Decimal("0.15")
    # Bullish decision + real positive move -> directionally correct.
    assert row.directional_correctness is True
    # Long call, strike 100, premium 2 -- breakeven is 102; underlying
    # settled at 115, well past it.
    assert row.breakeven_held is True


def test_bearish_direction_wrong_when_underlying_rises(db_session):
    decision = _seed_decision(db_session, "ZZFTD4", strategy_direction=DecisionDirection.BEARISH)
    entry = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=decision.benchmark_portfolio_id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("100"),
    )
    settlement = SettlementCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=decision.benchmark_portfolio_id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("110"),
    )
    db_session.add_all([entry, settlement])
    db_session.flush()

    row = build_forward_test_dataset_row(decision, entry, settlement)

    assert row.directional_correctness is False


def test_neutral_direction_never_graded(db_session):
    decision = _seed_decision(db_session, "ZZFTD5", strategy_direction=DecisionDirection.NEUTRAL)
    entry = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=decision.benchmark_portfolio_id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("100"),
    )
    settlement = SettlementCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=decision.benchmark_portfolio_id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("110"),
    )
    db_session.add_all([entry, settlement])
    db_session.flush()

    row = build_forward_test_dataset_row(decision, entry, settlement)

    assert row.directional_correctness is None


def test_breakeven_not_held_when_underlying_stays_below_strike(db_session):
    decision = _seed_decision(db_session, "ZZFTD6")
    entry = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=decision.benchmark_portfolio_id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("100"),
    )
    settlement = SettlementCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=decision.benchmark_portfolio_id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("101"),  # below the 102 breakeven
    )
    db_session.add_all([entry, settlement])
    db_session.flush()

    row = build_forward_test_dataset_row(decision, entry, settlement)

    assert row.breakeven_held is False


def test_list_forward_test_dataset_returns_newest_first(db_session):
    older = _seed_decision(db_session, "ZZFTD7", generated_at=datetime(2026, 9, 1, tzinfo=UTC))
    newer = _seed_decision(db_session, "ZZFTD8", generated_at=datetime(2026, 9, 20, tzinfo=UTC))

    rows = list_forward_test_dataset(db_session)

    ids = [r.decision_snapshot_id for r in rows]
    assert ids.index(newer.id) < ids.index(older.id)
