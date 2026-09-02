"""Phase 4 market-data-quality hardening (2026-08-26), Section 17 --
EntryCaptureAttempt/SettlementCaptureAttempt.market_data_quality_label.
"""

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
    EarningsTiming,
    MarketDataQuality,
    OptionAction,
    OptionType,
)

EXP = date(2026, 9, 18)


def _seed_attempt(db_session, ticker: str) -> EntryCaptureAttempt:
    event = EarningsCalendarEvent(
        symbol=ticker,
        company_name="ZZ MDQ Co",
        earnings_date=date(2026, 9, 17),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"{ticker} Portfolio", initial_capital=Decimal("2000"), cash_balance=Decimal("2000")
    )
    db_session.add_all([event, portfolio])
    db_session.flush()
    decision = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=ticker,
        company_name="ZZ MDQ Co",
        strategy_direction=DecisionDirection.BULLISH,
        generated_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        engine_version="test",
        prompt_version="test",
        expiration_source="test",
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
    return attempt


def _leg(attempt, quality: MarketDataQuality | None, leg_index: int = 0) -> EntrySnapshot:
    return EntrySnapshot(
        decision_id=attempt.decision_snapshot_id,
        capture_attempt_id=attempt.id,
        leg_index=leg_index,
        status=CaptureStatus.CAPTURED,
        expiration=EXP,
        strike=Decimal("100"),
        option_type=OptionType.CALL,
        action=OptionAction.BUY,
        quantity=1,
        multiplier=Decimal(100),
        market_data_quality=quality,
    )


def test_all_live_legs_is_verified_live(db_session):
    attempt = _seed_attempt(db_session, "ZZMDQ1")
    db_session.add(_leg(attempt, MarketDataQuality.LIVE))
    db_session.flush()
    db_session.refresh(attempt)

    assert attempt.market_data_quality_label == "VERIFIED_LIVE"


def test_any_delayed_leg_is_delayed_data(db_session):
    attempt = _seed_attempt(db_session, "ZZMDQ2")
    db_session.add_all(
        [_leg(attempt, MarketDataQuality.LIVE, 0), _leg(attempt, MarketDataQuality.DELAYED, 1)]
    )
    db_session.flush()
    db_session.refresh(attempt)

    assert attempt.market_data_quality_label == "DELAYED_DATA"


def test_missing_quality_is_unknown(db_session):
    attempt = _seed_attempt(db_session, "ZZMDQ3")
    db_session.add(_leg(attempt, None))
    db_session.flush()
    db_session.refresh(attempt)

    assert attempt.market_data_quality_label == "UNKNOWN_QUALITY"


def test_no_legs_is_unknown(db_session):
    attempt = _seed_attempt(db_session, "ZZMDQ4")
    db_session.flush()

    assert attempt.market_data_quality_label == "UNKNOWN_QUALITY"
