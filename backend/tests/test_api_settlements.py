"""Phase 4.5 -- GET /settlements/{decision_id}. Phase 4.6 (product
dashboard) -- GET /settlements (system-wide list). No mutation endpoint
exists -- see api/routers/settlements.py's own docstring.
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
from models.enums import CaptureStatus, DecisionDirection, DecisionSnapshotStatus, EarningsTiming
from models.exit_snapshot import ExitSnapshot
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


def _seed_full_settlement(db_session, symbol: str = "TESTAPI45"):
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="API Settlement Test Co",
        earnings_date=date(2026, 9, 16),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"API Settlement Test Portfolio {symbol}",
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

    entry_leg = EntrySnapshot(
        decision_id=decision.id,
        capture_attempt_id=entry_attempt.id,
        leg_index=0,
        status=CaptureStatus.CAPTURED,
        external_contract_id="111",
        strike=Decimal("100.00"),
        option_type="call",
        action="buy",
        quantity=1,
        multiplier=Decimal("100"),
        bid=Decimal("1.90"),
        ask=Decimal("2.10"),
        benchmark_entry_price=Decimal("2.10"),
        pricing_assumption="BUY_TO_OPEN_AT_ASK",
    )
    db_session.add(entry_leg)
    db_session.flush()

    settlement_attempt = SettlementCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        entry_capture_attempt_id=entry_attempt.id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("105.00"),
        realized_pnl=Decimal("80.00"),
        return_pct=Decimal("38.10"),
        r_multiple=Decimal("0.38"),
        is_win=True,
        captured_at=datetime(2026, 9, 17, 15, 55, tzinfo=UTC),
    )
    db_session.add(settlement_attempt)
    db_session.flush()

    exit_leg = ExitSnapshot(
        decision_id=decision.id,
        settlement_attempt_id=settlement_attempt.id,
        entry_snapshot_id=entry_leg.id,
        leg_index=0,
        status=CaptureStatus.CAPTURED,
        external_contract_id="111",
        strike=Decimal("100.00"),
        option_type="call",
        action="buy",
        quantity=1,
        multiplier=Decimal("100"),
        bid=Decimal("2.90"),
        ask=Decimal("3.10"),
        benchmark_exit_price=Decimal("2.90"),
        pricing_assumption="SELL_TO_CLOSE_AT_BID",
        realized_pnl_per_share=Decimal("0.80"),
    )
    db_session.add(exit_leg)
    db_session.flush()
    return decision, settlement_attempt, exit_leg


def test_settlements_endpoint_returns_the_captured_attempt_and_its_leg(client, db_session):
    decision, attempt, leg = _seed_full_settlement(db_session)

    response = client.get(f"/api/v1/settlements/{decision.id}")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == attempt.id
    assert body[0]["status"] == "captured"
    assert body[0]["realized_pnl"] == "80.00"
    assert body[0]["return_pct"] == "38.10"
    assert body[0]["r_multiple"] == "0.38"
    assert body[0]["is_win"] is True
    assert len(body[0]["legs"]) == 1
    assert body[0]["legs"][0]["id"] == leg.id
    assert body[0]["legs"][0]["benchmark_exit_price"] == "2.90"
    assert body[0]["legs"][0]["pricing_assumption"] == "SELL_TO_CLOSE_AT_BID"
    assert body[0]["legs"][0]["realized_pnl_per_share"] == "0.80"


def test_settlements_404_for_unknown_decision(client, db_session):
    response = client.get("/api/v1/settlements/999999999")
    assert response.status_code == 404


def test_settlements_lists_every_attempt_oldest_first_including_failed(client, db_session):
    decision, attempt, _leg = _seed_full_settlement(db_session, symbol="TESTAPI45B")
    failed_attempt = SettlementCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=attempt.benchmark_portfolio_id,
        entry_capture_attempt_id=attempt.entry_capture_attempt_id,
        status=CaptureStatus.FAILED,
        capture_error="a later retry that failed",
    )
    db_session.add(failed_attempt)
    db_session.flush()

    response = client.get(f"/api/v1/settlements/{decision.id}")
    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body] == [attempt.id, failed_attempt.id]
    assert body[1]["status"] == "failed"
    assert body[1]["capture_error"] == "a later retry that failed"


def test_settlements_returns_empty_list_for_a_decision_never_settled(client, db_session):
    event = EarningsCalendarEvent(
        symbol="TESTAPI45C",
        company_name="API Settlement Test Co C",
        earnings_date=date(2026, 9, 16),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name="API Settlement Test Portfolio C",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
    )
    db_session.add_all([event, portfolio])
    db_session.flush()
    decision = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker="TESTAPI45C",
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

    response = client.get(f"/api/v1/settlements/{decision.id}")
    assert response.status_code == 200
    assert response.json() == []


def test_list_all_settlements_endpoint(client, db_session):
    decision, attempt, _leg = _seed_full_settlement(db_session, symbol="TESTAPI45D")

    response = client.get("/api/v1/settlements")
    assert response.status_code == 200
    body = response.json()
    ids = {row["id"] for row in body}
    assert attempt.id in ids


def test_list_all_settlements_filters_by_status(client, db_session):
    decision, attempt, _leg = _seed_full_settlement(db_session, symbol="TESTAPI45E")

    response = client.get("/api/v1/settlements", params={"status": "captured"})
    assert response.status_code == 200
    body = response.json()
    assert all(row["status"] == "captured" for row in body)
    assert attempt.id in {row["id"] for row in body}

    response2 = client.get("/api/v1/settlements", params={"status": "failed"})
    assert response2.status_code == 200
    assert attempt.id not in {row["id"] for row in response2.json()}
