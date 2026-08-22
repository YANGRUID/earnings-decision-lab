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


class TestByMonthEndpoint:
    """Phase 4.9 -- GET /earnings-calendar/by-month, for a month-grid
    calendar UI. Registered before "/{symbol}" so "by-month" itself is
    never swallowed as if it were a ticker -- confirmed by these tests
    actually reaching the by-month handler at all."""

    def test_returns_events_within_the_given_month_regardless_of_status(self, client, db_session):
        db_session.add(
            EarningsCalendarEvent(
                symbol="TESTAUGA",
                company_name="Test August Co A",
                earnings_date=date(2026, 8, 5),
                earnings_time=EarningsTiming.BMO,
                status=EarningsCalendarEventStatus.UPCOMING,
            )
        )
        db_session.add(
            EarningsCalendarEvent(
                symbol="TESTAUGB",
                company_name="Test August Co B (already reported)",
                earnings_date=date(2026, 8, 20),
                earnings_time=EarningsTiming.AMC,
                status=EarningsCalendarEventStatus.COMPLETED,
            )
        )
        db_session.flush()

        response = client.get(
            "/api/v1/earnings-calendar/by-month", params={"year": 2026, "month": 8}
        )

        assert response.status_code == 200
        symbols = {row["symbol"] for row in response.json()}
        assert "TESTAUGA" in symbols
        # unlike the bare list endpoint, status doesn't filter this out
        assert "TESTAUGB" in symbols

    def test_excludes_events_outside_the_month(self, client, db_session):
        db_session.add(
            EarningsCalendarEvent(
                symbol="TESTJULY",
                company_name="Test July Co",
                earnings_date=date(2026, 7, 31),
                earnings_time=EarningsTiming.BMO,
            )
        )
        db_session.add(
            EarningsCalendarEvent(
                symbol="TESTSEPT",
                company_name="Test September Co",
                earnings_date=date(2026, 9, 1),
                earnings_time=EarningsTiming.BMO,
            )
        )
        db_session.flush()

        response = client.get(
            "/api/v1/earnings-calendar/by-month", params={"year": 2026, "month": 8}
        )

        symbols = {row["symbol"] for row in response.json()}
        assert "TESTJULY" not in symbols
        assert "TESTSEPT" not in symbols

    def test_empty_list_for_a_real_month_with_no_synced_data(self, client, db_session):
        response = client.get(
            "/api/v1/earnings-calendar/by-month", params={"year": 2019, "month": 3}
        )

        assert response.status_code == 200
        assert response.json() == []

    def test_invalid_month_is_rejected(self, client):
        response = client.get(
            "/api/v1/earnings-calendar/by-month", params={"year": 2026, "month": 13}
        )

        assert response.status_code == 422

    def test_february_leap_year_boundary(self, client, db_session):
        db_session.add(
            EarningsCalendarEvent(
                symbol="TESTLEAP",
                company_name="Test Leap Day Co",
                earnings_date=date(2028, 2, 29),  # 2028 is a real leap year
                earnings_time=EarningsTiming.BMO,
            )
        )
        db_session.flush()

        response = client.get(
            "/api/v1/earnings-calendar/by-month", params={"year": 2028, "month": 2}
        )

        symbols = {row["symbol"] for row in response.json()}
        assert "TESTLEAP" in symbols
