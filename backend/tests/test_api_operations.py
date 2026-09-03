"""GET /operations/* -- the V4-only Live Operations payloads (2026-09-02)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from core.config import get_settings


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


PLATFORM_JOB_IDS = {
    "earnings_calendar_sync",
    "earnings_research_preparation",
    "research_readiness_catchup",
    "research_preparation_startup_catchup",
    "ibkr_gateway_healthcheck",
}
SHADOW_JOB_IDS = {"v4_forward_window", "v4_shadow_decision", "v4_shadow_settlement"}


class TestOperationsSummary:
    def test_summary_carries_v4_blocks_only(self, client):
        response = client.get("/api/v1/operations/summary")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "health",
            "today",
            "readiness",
            "preflight",
            "market_clock",
            "staleness",
            "forward_window",
        }
        assert body["today"]["decision_window_et"] == "15:30"
        assert body["today"]["settlement_window_et"] == "15:30"
        assert body["today"]["deadline_et"] == "15:50"
        assert set(body["readiness"]) >= {
            "upcoming_events",
            "business_eligible",
            "company_resolved",
            "research_queued",
            "research_running",
            "research_ready",
            "research_failed",
            "ai_thesis_ready",
            "v4_decision_ready",
        }
        v4 = body["health"]["v4_shadow"]
        assert v4["decision_time_et"] == "15:30" and v4["settlement_time_et"] == "15:30"
        assert v4["timing_policy_version"] == "v4-1530-entry-1530-t1-settlement-v2"
        assert "official_run" not in body and "execution_summary" not in body


class TestOperationsEvents:
    def test_events_use_v4_lifecycle_vocabulary(self, client):
        response = client.get("/api/v1/operations/events")
        assert response.status_code == 200
        for ev in response.json()["events"]:
            assert ev["lifecycle_state"].isupper()
            assert "shadow_decision_id" in ev and "research_ready" in ev
            assert "decision_snapshot_id" not in ev


class TestOperationsJobs:
    def test_lists_platform_jobs_plus_the_forward_window_and_its_phases_when_enabled(self, client):
        response = client.get("/api/v1/operations/jobs")
        assert response.status_code == 200
        job_ids = {j["job_id"] for j in response.json()["jobs"]}
        expected = PLATFORM_JOB_IDS | SHADOW_JOB_IDS
        assert (
            job_ids == expected if get_settings().v4_shadow_enabled else job_ids >= PLATFORM_JOB_IDS
        )
        assert "decision_and_entry_capture" not in job_ids and "exit_capture" not in job_ids


class TestOperationsEventsForwardOnly:
    def test_default_view_hides_pre_v4_windows_and_include_past_shows_them(self, client):
        default = client.get("/api/v1/operations/events").json()["events"]
        complete = client.get("/api/v1/operations/events?include_past=true").json()["events"]
        assert len(default) <= len(complete)
        for row in default:
            assert row["shadow_decision_id"] is not None or row["lifecycle_state"] not in (
                "DECISION_WINDOW_MISSED",
                "RESEARCH_NOT_READY",
            )


class TestOperationsFailures:
    def test_returns_a_real_list_shape(self, client):
        response = client.get("/api/v1/operations/failures")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body["failures"], list)
        for failure in body["failures"]:
            assert failure["retryability"] in ("RETRYABLE", "NOT_RETRYABLE", "WINDOW_MISSED")


class TestOperationsHealth:
    def test_matches_the_summarys_own_health_block(self, client):
        summary = client.get("/api/v1/operations/summary").json()
        health = client.get("/api/v1/operations/health").json()
        assert health["scheduler"]["running"] == summary["health"]["scheduler"]["running"]
        assert health["ibkr"]["state"] == summary["health"]["ibkr"]["state"]

    def test_retired_diagnostics_are_gone(self, client):
        assert client.get("/api/v1/operations/quote-diagnostics/summary").status_code == 404
