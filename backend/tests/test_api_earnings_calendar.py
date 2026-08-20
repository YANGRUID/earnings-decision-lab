"""Phase 4.2 -- GET /earnings-calendar and GET /earnings-calendar/{symbol}.
Mounted separately from GET /earnings/{event_id} (api/routers/earnings.py)
-- see api/routers/earnings_calendar.py's own docstring for why the same
prefix would have collided.
"""

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsTiming


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


def test_calendar_endpoint_returns_upcoming_earnings(client, db_session):
    db_session.add(
        EarningsCalendarEvent(
            symbol="TESTAAPL",
            company_name="Test Apple Co",
            earnings_date=date(2026, 10, 29),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
    )
    db_session.add(
        EarningsCalendarEvent(
            symbol="TESTOLD",
            company_name="Test Already Reported Co",
            earnings_date=date(2025, 1, 1),
            earnings_time=EarningsTiming.BMO,
            status=EarningsCalendarEventStatus.COMPLETED,
        )
    )
    db_session.flush()

    response = client.get("/api/v1/earnings-calendar")
    assert response.status_code == 200
    body = response.json()
    symbols = {row["symbol"] for row in body}
    assert "TESTAAPL" in symbols
    # COMPLETED events aren't "upcoming" -- shouldn't show up here.
    assert "TESTOLD" not in symbols


def test_symbol_endpoint_returns_single_company_events(client, db_session):
    db_session.add(
        EarningsCalendarEvent(
            symbol="TESTMU",
            company_name="Test Micron Technology",
            earnings_date=date(2026, 9, 23),
            earnings_time=EarningsTiming.BMO,
        )
    )
    db_session.flush()

    response = client.get("/api/v1/earnings-calendar/testmu")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "TESTMU"
    assert body[0]["earnings_time"] == "bmo"


def test_symbol_endpoint_empty_list_for_unknown_symbol(client, db_session):
    response = client.get("/api/v1/earnings-calendar/NOSUCHSYMBOLXYZ")
    assert response.status_code == 200
    assert response.json() == []
