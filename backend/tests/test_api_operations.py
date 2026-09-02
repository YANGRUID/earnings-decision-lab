"""API-level coverage for the Live Operations Monitor. Real app +
real (test) scheduler via TestClient's own lifespan, dependency-
overridden db for the rollback-safe db_session -- mirrors tests/
test_api_provider_settings.py's own established pattern exactly.

The one security property checked explicitly here, per the brief this
router was built against: no operations endpoint ever returns an IBKR
account identifier, a password, an API key, a session id, or an
authorization header -- only real, already-safe aggregate/status values.
"""

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


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


def _assert_no_sensitive_leak(response_json: dict) -> None:
    text = json.dumps(response_json).lower()
    for banned in ("password", "session_id", "sessionid", "authorization", "bearer "):
        assert banned not in text, f"leaked {banned!r} in operations response"


class TestOperationsSummary:
    def test_returns_200_with_real_structure(self, client):
        response = client.get("/api/v1/operations/summary")
        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {
            "health",
            "execution_summary",
            "official_run",
            "preflight",
            "market_clock",
        }
        assert set(body["health"].keys()) == {
            "ibkr",
            "earnings_calendar",
            "ai_provider",
            "scheduler",
            "database",
            # V4.4C -- the EXPERIMENTAL shadow cohort's own health domain,
            # reported alongside the official ones and never merged into
            # them. Added deliberately: a shadow failure must be visible
            # without being able to degrade an official domain.
            "v4_shadow",
        }
        _assert_no_sensitive_leak(body)

    def test_ibkr_health_never_exposes_an_account_id(self, client):
        response = client.get("/api/v1/operations/summary")
        body = response.json()
        ibkr = body["health"]["ibkr"]
        assert "account_id" not in ibkr
        assert "account" not in ibkr
        # only a real boolean, per the brief's explicit "live vs paper"
        # requirement -- never the identifier itself.
        assert ibkr["live_account"] in (True, False, None)

    def test_market_clock_reports_three_real_timezones(self, client):
        response = client.get("/api/v1/operations/summary")
        clock = response.json()["market_clock"]
        assert clock["utc_now"] is not None
        assert clock["new_york_now"] is not None
        assert clock["zurich_now"] is not None
        assert clock["market_session"] in ("pre_market", "regular", "after_hours", "closed")

    def test_database_health_is_a_real_check_not_a_hardcoded_value(self, client):
        response = client.get("/api/v1/operations/summary")
        database = response.json()["health"]["database"]
        # Real `SELECT 1` against the real test DB, and the real Alembic
        # head row -- not the hardcoded True/True/None this field
        # previously always returned regardless of actual DB state.
        assert database["backend_healthy"] is True
        assert database["database_healthy"] is True
        assert database["migration_head"] is not None
        assert len(database["migration_head"]) > 0

    def test_preflight_reports_ready_and_a_real_checklist(self, client):
        response = client.get("/api/v1/operations/summary")
        preflight = response.json()["preflight"]
        assert isinstance(preflight["ready"], bool)
        assert isinstance(preflight["blockers"], list)
        labels = {c["label"] for c in preflight["checks"]}
        assert "IBKR authenticated" in labels
        assert "Live account confirmed" in labels
        assert "Benchmark portfolio active" in labels


class TestOperationsEvents:
    def test_returns_a_real_list_shape(self, client):
        response = client.get("/api/v1/operations/events")
        assert response.status_code == 200
        body = response.json()
        assert "events" in body
        assert isinstance(body["events"], list)
        if body["events"]:
            event = body["events"][0]
            assert "lifecycle_state" in event
            assert "timeline" in event
            assert isinstance(event["timeline"], list)


OFFICIAL_JOB_IDS = {
    "earnings_calendar_sync",
    "earnings_research_preparation",
    "research_readiness_catchup",
    "research_preparation_startup_catchup",
    "ibkr_gateway_healthcheck",
}
SHADOW_JOB_IDS = {"v4_shadow_decision", "v4_shadow_settlement"}


class TestOperationsJobs:
    def test_lists_the_five_official_jobs_plus_the_shadow_pair_only_when_enabled(self, client):
        """The monitor reports every job the live scheduler has registered:
        the platform jobs always, and the two V4 shadow jobs exactly when
        V4_SHADOW_ENABLED is on (activated in production 2026-09-02)."""
        from core.config import get_settings

        response = client.get("/api/v1/operations/jobs")
        assert response.status_code == 200
        job_ids = {j["job_id"] for j in response.json()["jobs"]}
        shadow = SHADOW_JOB_IDS if get_settings().v4_shadow_enabled else set()
        expected = OFFICIAL_JOB_IDS | shadow
        assert job_ids == expected


class TestOperationsFailures:
    def test_returns_a_real_list_shape(self, client):
        response = client.get("/api/v1/operations/failures")
        assert response.status_code == 200
        body = response.json()
        assert "failures" in body
        assert isinstance(body["failures"], list)
        for failure in body["failures"]:
            assert failure["retryability"] in ("RETRYABLE", "NOT_RETRYABLE", "WINDOW_MISSED")


class TestOperationsHealth:
    def test_matches_the_summarys_own_health_block(self, client):
        summary = client.get("/api/v1/operations/summary").json()
        health = client.get("/api/v1/operations/health").json()
        assert health["scheduler"]["running"] == summary["health"]["scheduler"]["running"]
        assert health["ibkr"]["state"] == summary["health"]["ibkr"]["state"]


class TestQuoteDiagnosticsEndpoints:
    """Phase 4 quote-observability hardening (2026-08-26), Sections 13-14."""

    def test_summary_returns_200_with_real_structure(self, client):
        response = client.get("/api/v1/operations/quote-diagnostics/summary")
        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {
            "window_hours",
            "contracts_requested",
            "contracts_resolved",
            "total_snapshot_attempts",
            "average_attempts_per_leg",
            "median_attempts_per_leg",
            "quote_unavailable_count",
            "rate_limited_count",
            "permission_error_count",
            "contract_error_count",
        }
        _assert_no_sensitive_leak(body)

    def test_entry_diagnostics_404_when_no_telemetry_exists(self, client):
        response = client.get("/api/v1/operations/quote-diagnostics/entry/999999")
        assert response.status_code == 404

    def test_settlement_diagnostics_404_when_no_telemetry_exists(self, client):
        response = client.get("/api/v1/operations/quote-diagnostics/settlement/999999")
        assert response.status_code == 404

    def test_entry_diagnostics_returns_real_leg_data(self, db_session, client):
        from datetime import UTC, date, datetime
        from decimal import Decimal

        from models.benchmark_portfolio import BenchmarkPortfolio
        from models.decision_snapshot import DecisionSnapshot
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.entry_capture_attempt import EntryCaptureAttempt
        from models.enums import (
            CaptureStatus,
            DecisionDirection,
            EarningsTiming,
            OptionType,
            QuoteAcquisitionCaptureType,
            QuoteRequirement,
        )
        from models.quote_acquisition_attempt import QuoteAcquisitionAttempt

        event = EarningsCalendarEvent(
            symbol="ZZAPID",
            company_name="ZZ API Diag Co",
            earnings_date=date(2026, 9, 17),
            earnings_time=EarningsTiming.AMC,
        )
        portfolio = BenchmarkPortfolio(
            name="ZZAPID Portfolio", initial_capital=Decimal("2000"), cash_balance=Decimal("2000")
        )
        db_session.add_all([event, portfolio])
        db_session.flush()
        decision = DecisionSnapshot(
            earnings_calendar_event_id=event.id,
            benchmark_portfolio_id=portfolio.id,
            ticker="ZZAPID",
            company_name="ZZ API Diag Co",
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
            status=CaptureStatus.FAILED,
            capture_error="no ask quote available for a long leg",
        )
        db_session.add(attempt)
        db_session.flush()
        db_session.add(
            QuoteAcquisitionAttempt(
                capture_attempt_type=QuoteAcquisitionCaptureType.ENTRY,
                entry_capture_attempt_id=attempt.id,
                ticker="ZZAPID",
                leg_index=0,
                expiration=date(2026, 9, 18),
                option_type=OptionType.CALL,
                strike=Decimal("100"),
                required_side=QuoteRequirement.ASK,
                snapshot_attempt_number=1,
                observed_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
                elapsed_ms=1500,
                bid_present=True,
                ask_present=False,
                last_present=True,
                contract_resolved=True,
                final_for_leg=True,
            )
        )
        db_session.flush()

        response = client.get(f"/api/v1/operations/quote-diagnostics/entry/{attempt.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["ticker"] == "ZZAPID"
        assert len(body["legs"]) == 1
        assert body["legs"][0]["required_side"] == "ask"
        assert body["legs"][0]["result_label"] == "ASK unavailable after bounded retry"
        _assert_no_sensitive_leak(body)


class TestOperationsIsReadOnly:
    """The brief's own explicit Section 16 requirement -- no mutation
    endpoint of any kind under this router."""

    def test_no_post_put_patch_delete_route_exists(self, client):
        from api.main import app

        operations_routes = [
            r for r in app.routes if getattr(r, "path", "").startswith("/api/v1/operations")
        ]
        for route in operations_routes:
            methods = getattr(route, "methods", set()) or set()
            assert methods <= {"GET", "HEAD", "OPTIONS"}, (
                f"operations route {route.path} exposes a mutating method: {methods}"
            )
