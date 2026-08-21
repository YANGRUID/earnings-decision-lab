"""Phase 4.4 -- GET /decision-snapshots/{id}/entries and
GET /benchmark/entries. No mutation endpoint exists for either -- see
api/routers/decision_snapshots.py's own docstring.
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


def _seed_full_entry(db_session, symbol: str = "TESTAPI44"):
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="API Test Co",
        earnings_date=date(2026, 9, 16),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"API Test Portfolio {symbol}",
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

    attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=CaptureStatus.CAPTURED,
        underlying_price=Decimal("100.00"),
        net_entry_cash=Decimal("200.00"),
        contracts=1,
        initial_max_risk=Decimal("200.00"),
        capital_utilization=Decimal("10.00"),
        captured_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
    )
    db_session.add(attempt)
    db_session.flush()

    leg = EntrySnapshot(
        decision_id=decision.id,
        capture_attempt_id=attempt.id,
        leg_index=0,
        status=CaptureStatus.CAPTURED,
        strike=Decimal("100.00"),
        option_type="call",
        action="buy",
        quantity=1,
        bid=Decimal("1.90"),
        ask=Decimal("2.10"),
        benchmark_entry_price=Decimal("2.10"),
        pricing_assumption="BUY_TO_OPEN_AT_ASK",
    )
    db_session.add(leg)
    db_session.flush()
    return decision, attempt, leg


def test_decision_snapshot_entries_endpoint(client, db_session):
    decision, attempt, leg = _seed_full_entry(db_session)

    response = client.get(f"/api/v1/decision-snapshots/{decision.id}/entries")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == attempt.id
    assert body[0]["status"] == "captured"
    assert len(body[0]["legs"]) == 1
    assert body[0]["legs"][0]["benchmark_entry_price"] == "2.10"
    assert body[0]["legs"][0]["pricing_assumption"] == "BUY_TO_OPEN_AT_ASK"


def test_decision_snapshot_entries_404_for_unknown_decision(client, db_session):
    response = client.get("/api/v1/decision-snapshots/999999999/entries")
    assert response.status_code == 404


def test_benchmark_entries_list_endpoint(client, db_session):
    decision, attempt, _leg = _seed_full_entry(db_session, symbol="TESTAPI44B")

    response = client.get("/api/v1/benchmark/entries")
    assert response.status_code == 200
    body = response.json()
    ids = {row["id"] for row in body}
    assert attempt.id in ids


def test_benchmark_entries_filters_by_status(client, db_session):
    decision, attempt, _leg = _seed_full_entry(db_session, symbol="TESTAPI44C")

    response = client.get("/api/v1/benchmark/entries", params={"status": "captured"})
    assert response.status_code == 200
    body = response.json()
    assert all(row["status"] == "captured" for row in body)
    assert attempt.id in {row["id"] for row in body}

    response2 = client.get("/api/v1/benchmark/entries", params={"status": "failed"})
    assert response2.status_code == 200
    assert attempt.id not in {row["id"] for row in response2.json()}
