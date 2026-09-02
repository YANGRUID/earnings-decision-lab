"""Phase 4.9 -- POST /admin/run-earnings-sync,
/admin/run-decision-generation, /admin/run-settlement-capture.

The underlying job functions (services/scheduler.py) each open their
own real SessionLocal() and commit independently of this test suite's
own rollback-wrapped db_session fixture -- calling them unmocked here
would write real, permanent rows into whatever database DATABASE_URL
points at (this project's tests run against a real local Postgres, not
a disposable one; see tests/conftest.py), and would make a real network
call to Finnhub/IBKR on every `pytest` run. Neither is acceptable for a
unit test, so every test here monkeypatches the job function itself and
verifies the router's own wiring (which function it calls, how it
computes before/after counts, the production gate) -- the job
functions' own real behavior already has its own test coverage
elsewhere (e.g. test_services_scheduler.py, test_services_decision_
pipeline.py).
"""

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func

from core.config import Settings
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsSource, EarningsTiming
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


class TestAdminEndpointsInDevelopment:
    def test_run_earnings_sync_calls_the_real_scheduler_job_and_reports_counts(
        self, client, db_session, monkeypatch
    ):
        import api.routers.admin as admin_module

        called = []
        monkeypatch.setattr(
            admin_module,
            "run_earnings_calendar_sync_job",
            lambda from_date=None: called.append(from_date),
        )
        before = db_session.query(func.count(EarningsCalendarEvent.id)).scalar() or 0

        response = client.post("/api/v1/admin/run-earnings-sync")

        # no from_date query param -- the exact same behavior the cron trigger gets
        assert called == [None]
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "earnings_calendar_events_before": before,
            "earnings_calendar_events_after": before,  # the mock touched nothing
        }

    def test_run_decision_generation_calls_the_real_scheduler_job_and_reports_counts(
        self, client, db_session, monkeypatch
    ):
        import api.routers.admin as admin_module

        called = []
        monkeypatch.setattr(
            admin_module, "run_decision_and_entry_capture_job", lambda: called.append(True)
        )
        before = db_session.query(func.count(DecisionSnapshot.id)).scalar() or 0

        response = client.post("/api/v1/admin/run-decision-generation")

        assert called == [True]
        assert response.status_code == 200
        body = response.json()
        assert body["decision_snapshots_before"] == before
        assert body["decision_snapshots_after"] == before
        assert "entry_capture_attempts_before" in body
        assert "entry_capture_attempts_after" in body

    def test_run_settlement_capture_calls_the_real_scheduler_job_and_reports_counts(
        self, client, db_session, monkeypatch
    ):
        import api.routers.admin as admin_module

        called = []
        monkeypatch.setattr(admin_module, "run_exit_capture_job", lambda: called.append(True))
        before = db_session.query(func.count(SettlementCaptureAttempt.id)).scalar() or 0

        response = client.post("/api/v1/admin/run-settlement-capture")

        assert called == [True]
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "settlement_capture_attempts_before": before,
            "settlement_capture_attempts_after": before,
        }

    def test_run_research_preparation_enqueues_only_and_returns_202_with_real_counts(
        self, client, monkeypatch
    ):
        """Pre-live hardening (2026-08-25): this endpoint no longer owns
        the lifetime of any long-running preparation work -- it only
        enqueues durable ResearchPreparationJob rows (services/
        earnings_research_preparation.py::enqueue_preparation_
        candidates, via run_earnings_research_preparation_job) and
        returns immediately with the real per-candidate outcome counts,
        HTTP 202 (accepted, not yet done), never 200 (implying the real
        preparation work itself already finished)."""
        import api.routers.admin as admin_module
        from services.earnings_research_preparation import EnqueueResult

        called = []
        fake_results = [
            EnqueueResult(1, "TESTQ", "queued", None),
            EnqueueResult(2, "TESTQ2", "queued", None),
            EnqueueResult(3, "TESTREADY", "already_ready", None),
            EnqueueResult(4, "TESTSMALL", "filtered_out", "market cap too low"),
            EnqueueResult(5, "TESTWARN", "preparation_warning", "options chain lookup failed"),
        ]
        monkeypatch.setattr(
            admin_module,
            "run_earnings_research_preparation_job",
            lambda: (called.append(True), fake_results)[1],
        )

        response = client.post("/api/v1/admin/run-research-preparation")

        assert called == [True]
        assert response.status_code == 202
        body = response.json()
        assert body == {
            "queued": 2,
            "already_ready": 1,
            "filtered_out": 1,
            "preparation_warning": 1,
        }

    def test_a_job_that_actually_adds_rows_is_reflected_in_the_after_count(
        self, client, db_session, monkeypatch
    ):
        """Confirms the before/after counts are real, live queries, not
        a hardcoded echo of the before count -- the one thing the three
        tests above can't tell apart on their own, since their mocks
        touch nothing."""
        import api.routers.admin as admin_module

        def _fake_sync(from_date=None) -> None:
            db_session.add(
                EarningsCalendarEvent(
                    symbol="ZZADMIN1",
                    company_name="Admin Test Co",
                    earnings_date=date(2026, 9, 1),
                    earnings_time=EarningsTiming.AMC,
                    source=EarningsSource.FINNHUB,
                )
            )
            db_session.flush()

        monkeypatch.setattr(admin_module, "run_earnings_calendar_sync_job", _fake_sync)
        before = db_session.query(func.count(EarningsCalendarEvent.id)).scalar() or 0

        response = client.post("/api/v1/admin/run-earnings-sync")

        body = response.json()
        assert body["earnings_calendar_events_before"] == before
        assert body["earnings_calendar_events_after"] == before + 1


class TestAdminEndpointsDisabledInProduction:
    def test_routes_do_not_exist_when_app_env_is_production(self, monkeypatch):
        import api.main as main_module

        monkeypatch.setattr(
            main_module,
            "get_settings",
            lambda: Settings(app_env="production", _env_file=None),
        )
        prod_app = main_module.create_app()

        with TestClient(prod_app) as prod_client:
            response = prod_client.post("/api/v1/admin/run-earnings-sync")

        assert response.status_code == 404

    def test_case_insensitive_production_check(self, monkeypatch):
        import api.main as main_module

        monkeypatch.setattr(
            main_module,
            "get_settings",
            lambda: Settings(app_env="PRODUCTION", _env_file=None),
        )
        prod_app = main_module.create_app()

        with TestClient(prod_app) as prod_client:
            response = prod_client.post("/api/v1/admin/run-decision-generation")

        assert response.status_code == 404

    def test_routes_exist_normally_in_development(self, monkeypatch):
        import api.main as main_module
        import api.routers.admin as admin_module

        monkeypatch.setattr(
            main_module,
            "get_settings",
            lambda: Settings(app_env="development", _env_file=None),
        )
        # The job itself is mocked here too, same reasoning as every
        # other test in this file -- this test only asserts the route is
        # registered at all (not 404), never that a real job ran.
        monkeypatch.setattr(
            admin_module, "run_earnings_calendar_sync_job", lambda from_date=None: None
        )
        dev_app = main_module.create_app()

        with TestClient(dev_app) as dev_client:
            response = dev_client.post("/api/v1/admin/run-earnings-sync")

        assert response.status_code == 200
