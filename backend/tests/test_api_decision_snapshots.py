"""Phase 4.3 -- GET /decision-snapshots and GET /decision-snapshots/{id}.
No mutation endpoint exists for this table -- see
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
from models.enums import DecisionDirection, DecisionSnapshotStatus, EarningsTiming


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


def _seed_snapshot(db_session, symbol: str = "TESTAPI4", **overrides) -> DecisionSnapshot:
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="API Test Co",
        earnings_date=date(2026, 9, 17),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"API Test Portfolio {symbol}",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
    )
    db_session.add_all([event, portfolio])
    db_session.flush()

    defaults = dict(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=symbol,
        company_name="API Test Co",
        strategy_direction=DecisionDirection.BULLISH,
        strategy_type="long_call",
        generated_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        status=DecisionSnapshotStatus.PENDING_ENTRY,
        engine_version="options-decision-engine-v3",
        prompt_version="v1",
        expiration_source="v3_auto_resolver",
    )
    defaults.update(overrides)
    snapshot = DecisionSnapshot(**defaults)
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


def test_list_endpoint_returns_snapshots(client, db_session):
    _seed_snapshot(db_session, symbol="TESTAPI4A")

    response = client.get("/api/v1/decision-snapshots")
    assert response.status_code == 200
    body = response.json()
    tickers = {row["ticker"] for row in body}
    assert "TESTAPI4A" in tickers


def test_list_endpoint_filters_by_ticker(client, db_session):
    _seed_snapshot(db_session, symbol="TESTAPI4B")
    _seed_snapshot(db_session, symbol="TESTAPI4C")

    response = client.get("/api/v1/decision-snapshots", params={"ticker": "testapi4b"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["ticker"] == "TESTAPI4B"


def test_detail_endpoint_returns_full_snapshot(client, db_session):
    snapshot = _seed_snapshot(
        db_session,
        symbol="TESTAPI4D",
        why_this_strategy=["real reason"],
        legs=[{"option_type": "call", "action": "buy", "strike": "100", "premium": "2"}],
    )

    response = client.get(f"/api/v1/decision-snapshots/{snapshot.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == snapshot.id
    assert body["ticker"] == "TESTAPI4D"
    assert body["why_this_strategy"] == ["real reason"]
    assert body["legs"] == [
        {"option_type": "call", "action": "buy", "strike": "100", "premium": "2"}
    ]
    assert body["engine_version"] == "options-decision-engine-v3"


def test_detail_endpoint_404_for_unknown_id(client, db_session):
    response = client.get("/api/v1/decision-snapshots/999999999")
    assert response.status_code == 404
