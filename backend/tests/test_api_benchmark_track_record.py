"""Phase 4.6 -- GET /benchmark/track-record and GET /benchmark/
calibration. No mutation endpoint exists -- see api/routers/
benchmark_track_record.py's own docstring.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.entry_snapshot import EntrySnapshot
from models.enums import (
    CaptureStatus,
    DecisionDirection,
    DecisionSnapshotStatus,
    EarningsTiming,
    OptionAction,
    OptionType,
    RiskProfile,
)
from models.settlement_capture_attempt import SettlementCaptureAttempt


@pytest.fixture(scope="module")
def test_client() -> Iterator[TestClient]:
    from api.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(test_client, db_session) -> Iterator[TestClient]:
    from api.deps import get_db

    test_client.app.dependency_overrides[get_db] = lambda: db_session
    yield test_client
    test_client.app.dependency_overrides.clear()


def _seed_portfolio(db_session, symbol_prefix: str) -> BenchmarkPortfolio:
    portfolio = BenchmarkPortfolio(
        name=f"API Track Record Test Portfolio {symbol_prefix}",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
        risk_profile=RiskProfile.MODERATE,
        is_active=True,
    )
    db_session.add(portfolio)
    db_session.flush()
    return portfolio


def _seed_settled_decision(
    db_session,
    portfolio: BenchmarkPortfolio,
    symbol: str,
    *,
    engine_version: str = "options-decision-engine-v3",
):
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Co",
        earnings_date=date(2026, 9, 16),
        earnings_time=EarningsTiming.AMC,
    )
    db_session.add(event)
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
        selected_expiration=date(2026, 9, 18),
        estimated_probability=Decimal("0.72"),
        volatility_regime="normal",
        engine_version=engine_version,
        prompt_version="v1",
        expiration_source="v3_auto_resolver",
    )
    db_session.add(decision)
    db_session.flush()

    entry_attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("100.00"),
        net_entry_cash=Decimal("210.00"),
        contracts=1,
        initial_max_risk=Decimal("210.00"),
        captured_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
    )
    db_session.add(entry_attempt)
    db_session.flush()
    db_session.add(
        EntrySnapshot(
            decision_id=decision.id,
            capture_attempt_id=entry_attempt.id,
            leg_index=0,
            status=CaptureStatus.CAPTURED,
            strike=Decimal("100.00"),
            option_type=OptionType.CALL,
            action=OptionAction.BUY,
            quantity=1,
            multiplier=Decimal("100"),
            benchmark_entry_price=Decimal("2.10"),
            pricing_assumption="BUY_TO_OPEN_AT_ASK",
        )
    )
    settlement_attempt = SettlementCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        entry_capture_attempt_id=entry_attempt.id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("110.00"),
        realized_pnl=Decimal("80.00"),
        return_pct=Decimal("38.10"),
        r_multiple=Decimal("0.38"),
        is_win=True,
        captured_at=datetime(2026, 9, 17, 15, 55, tzinfo=UTC),
    )
    db_session.add(settlement_attempt)
    db_session.flush()
    return decision, entry_attempt, settlement_attempt


def test_track_record_with_zero_settled_trades_returns_null_metrics(client, db_session):
    portfolio = _seed_portfolio(db_session, "TESTAPITR46A")

    response = client.get("/api/v1/benchmark/track-record", params={"portfolio_id": portfolio.id})
    assert response.status_code == 200
    body = response.json()
    assert body["settled_decisions"] == 0
    assert body["win_rate"]["total"] == 0
    assert body["win_rate"]["pct"] is None
    assert body["average_r"] is None
    assert body["profit_factor"] is None
    assert body["max_drawdown"] is None


def test_track_record_with_a_settled_win(client, db_session):
    portfolio = _seed_portfolio(db_session, "TESTAPITR46B")
    _seed_settled_decision(db_session, portfolio, "TESTAPITR46B1")

    response = client.get("/api/v1/benchmark/track-record", params={"portfolio_id": portfolio.id})
    assert response.status_code == 200
    body = response.json()
    assert body["total_decisions"] == 1
    assert body["settled_decisions"] == 1
    assert body["win_rate"]["correct"] == 1
    assert body["win_rate"]["total"] == 1
    assert body["win_rate"]["pct"] == "1"
    assert body["directional_accuracy"]["correct"] == 1


def test_track_record_reports_standardized_metrics_alongside_legacy_ones(client, db_session):
    """V4.1 methodology foundation (2026-08-31) -- the new, correctly-
    labeled per-decision reading appears alongside V3's own unaltered
    legacy max_drawdown, never replacing it."""
    portfolio = _seed_portfolio(db_session, "TESTAPITR46F")
    _seed_settled_decision(db_session, portfolio, "TESTAPITR46F1")

    response = client.get("/api/v1/benchmark/track-record", params={"portfolio_id": portfolio.id})
    body = response.json()
    assert body["max_drawdown"] is not None  # V3's legacy figure, unchanged
    assert body["legacy_capital_caveat"] is not None
    assert "not a true portfolio equity curve" in body["legacy_capital_caveat"]
    assert body["standardized"]["n"] == 1
    assert body["standardized"]["wins"] == 1
    assert body["standardized"]["portfolio_drawdown_available"] is False
    assert Decimal(body["standardized"]["mean_return_on_standardized_capital"]) == Decimal("0.04")


def test_track_record_engine_version_filter_isolates_v3_from_v4_cohorts(client, db_session):
    """V4.1 Section 13 -- V3 and V4 must never be silently mixed into one
    aggregate. V4 genuinely has zero official decisions in this
    codebase; this test seeds a synthetic V4-labeled row purely to prove
    the FILTER mechanism isolates cohorts correctly -- it does not claim
    this represents any real V4 decision."""
    portfolio = _seed_portfolio(db_session, "TESTAPITR46G")
    _seed_settled_decision(
        db_session, portfolio, "TESTAPITR46G1", engine_version="options-decision-engine-v3"
    )
    _seed_settled_decision(
        db_session, portfolio, "TESTAPITR46G2", engine_version="options-decision-engine-v4"
    )

    unfiltered = client.get("/api/v1/benchmark/track-record", params={"portfolio_id": portfolio.id})
    v3_only = client.get(
        "/api/v1/benchmark/track-record",
        params={"portfolio_id": portfolio.id, "engine_version": "options-decision-engine-v3"},
    )
    v4_only = client.get(
        "/api/v1/benchmark/track-record",
        params={"portfolio_id": portfolio.id, "engine_version": "options-decision-engine-v4"},
    )

    assert unfiltered.json()["total_decisions"] == 2
    assert v3_only.json()["total_decisions"] == 1
    assert v3_only.json()["settled_decisions"] == 1
    assert v4_only.json()["total_decisions"] == 1
    assert v4_only.json()["settled_decisions"] == 1
    # Each cohort's own standardized summary reflects only its own
    # decisions, never the other cohort's.
    assert v3_only.json()["standardized"]["n"] == 1
    assert v4_only.json()["standardized"]["n"] == 1


def test_track_record_strategy_filter(client, db_session):
    portfolio = _seed_portfolio(db_session, "TESTAPITR46C")
    _seed_settled_decision(db_session, portfolio, "TESTAPITR46C1")

    matching = client.get(
        "/api/v1/benchmark/track-record",
        params={"portfolio_id": portfolio.id, "strategy": "long_call"},
    )
    non_matching = client.get(
        "/api/v1/benchmark/track-record",
        params={"portfolio_id": portfolio.id, "strategy": "iron_condor"},
    )
    assert matching.json()["total_decisions"] == 1
    assert non_matching.json()["total_decisions"] == 0


def test_track_record_rejects_an_unknown_confidence_bucket(client, db_session):
    portfolio = _seed_portfolio(db_session, "TESTAPITR46D")

    response = client.get(
        "/api/v1/benchmark/track-record",
        params={"portfolio_id": portfolio.id, "confidence_bucket": "not-a-real-bucket"},
    )
    assert response.status_code == 422


def test_track_record_404_for_unknown_portfolio(client, db_session):
    response = client.get("/api/v1/benchmark/track-record", params={"portfolio_id": 999_999_999})
    assert response.status_code == 404


def test_calibration_with_zero_settled_trades_returns_empty_buckets(client, db_session):
    portfolio = _seed_portfolio(db_session, "TESTAPITR46E")

    response = client.get("/api/v1/benchmark/calibration", params={"portfolio_id": portfolio.id})
    assert response.status_code == 200
    body = response.json()
    assert body["settled_decisions"] == 0
    assert len(body["buckets"]) == 5
    assert all(bucket["rate"]["pct"] is None for bucket in body["buckets"])


def test_calibration_buckets_a_real_settled_decision(client, db_session):
    portfolio = _seed_portfolio(db_session, "TESTAPITR46F")
    _seed_settled_decision(db_session, portfolio, "TESTAPITR46F1")

    response = client.get("/api/v1/benchmark/calibration", params={"portfolio_id": portfolio.id})
    assert response.status_code == 200
    body = response.json()
    assert body["settled_decisions"] == 1
    by_label = {b["label"]: b for b in body["buckets"]}
    assert by_label["70-80%"]["rate"]["correct"] == 1
    assert by_label["70-80%"]["rate"]["total"] == 1


def test_calibration_404_for_unknown_portfolio(client, db_session):
    response = client.get("/api/v1/benchmark/calibration", params={"portfolio_id": 999_999_999})
    assert response.status_code == 404
