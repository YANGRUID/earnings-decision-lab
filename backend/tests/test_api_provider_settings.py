"""API-level coverage for the Data Provider Control Center: real dashboard
shape, real primary/fallback validation, and the security property that
matters most here -- no endpoint under this router ever returns a full,
unmasked API key, regardless of what's configured in the real environment
this test process happens to run in.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import api.routers.provider_settings as provider_settings_router
from models.enums import ProviderHealthStatus
from models.provider_health_event import ProviderHealthEvent


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


def _all_provider_rows(body: dict) -> list[dict]:
    return [p for domain in body["domains"] for p in domain["providers"]]


class TestGetDashboard:
    def test_returns_all_five_real_domains(self, client):
        response = client.get("/api/v1/settings/providers")
        assert response.status_code == 200
        domains = {d["domain"] for d in response.json()["domains"]}
        assert domains == {
            "price_history",
            "earnings_estimates",
            "filings",
            "options",
            "llm",
        }

    def test_no_provider_row_ever_exposes_a_full_unmasked_key(self, client):
        response = client.get("/api/v1/settings/providers")
        for provider in _all_provider_rows(response.json()):
            masked = provider["masked_key"]
            if masked is not None:
                # The only real shape a masked key may take: bullets, then
                # at most the last 4 real characters -- never the full value.
                assert masked.startswith("•") or len(masked) <= 4

    def test_stooq_never_appears_as_an_available_provider(self, client):
        # providers/stooq.py exists in the codebase but is dead (blocked by
        # robots.txt) -- it must never be presented as a real choice.
        response = client.get("/api/v1/settings/providers")
        provider_names = {p["provider"] for p in _all_provider_rows(response.json())}
        assert "stooq" not in provider_names


class TestUpdateSettings:
    def test_valid_selection_is_persisted_and_reflected_on_the_next_get(self, client):
        put_response = client.put(
            "/api/v1/settings/providers", json={"price_history_primary": "alpha_vantage"}
        )
        assert put_response.status_code == 200
        assert put_response.json()["domains"]
        price_history = next(
            d for d in put_response.json()["domains"] if d["domain"] == "price_history"
        )
        assert price_history["primary"] == "alpha_vantage"
        assert price_history["primary_is_override"] is True

        get_response = client.get("/api/v1/settings/providers")
        price_history = next(
            d for d in get_response.json()["domains"] if d["domain"] == "price_history"
        )
        assert price_history["primary"] == "alpha_vantage"

    def test_unknown_provider_name_is_rejected_with_422_and_not_persisted(self, client):
        response = client.put(
            "/api/v1/settings/providers", json={"price_history_primary": "made_up_provider"}
        )
        assert response.status_code == 422

        get_response = client.get("/api/v1/settings/providers")
        price_history = next(
            d for d in get_response.json()["domains"] if d["domain"] == "price_history"
        )
        assert price_history["primary_is_override"] is False

    def test_clear_fallback_flag_nulls_a_previously_set_fallback(self, client):
        # An explicit primary is set too, so clearing the fallback doesn't
        # get masked by the implicit "no primary override -> default to
        # alpha_vantage fallback" rule (see services/provider_status.py's
        # _resolve_price_history) -- this isolates the clear behavior itself.
        client.put(
            "/api/v1/settings/providers",
            json={"price_history_primary": "tiingo", "price_history_fallback": "alpha_vantage"},
        )
        response = client.put(
            "/api/v1/settings/providers", json={"clear_price_history_fallback": True}
        )
        price_history = next(
            d for d in response.json()["domains"] if d["domain"] == "price_history"
        )
        assert price_history["fallback"] is None
        assert price_history["fallback_is_override"] is False


class TestTestConnection:
    def test_unknown_domain_provider_pair_returns_404(self, client):
        response = client.post("/api/v1/settings/providers/price_history/ibkr/test")
        assert response.status_code == 404

    def test_successful_check_returns_connected_and_records_a_health_event(
        self, client, db_session, monkeypatch
    ):
        monkeypatch.setattr(
            provider_settings_router,
            "test_connection",
            lambda settings, provider, domain, db=None: (ProviderHealthStatus.CONNECTED, None),
        )
        response = client.post("/api/v1/settings/providers/price_history/tiingo/test")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "connected"
        assert body["provider"] == "tiingo"
        assert body["domain"] == "price_history"

        events = (
            db_session.query(ProviderHealthEvent)
            .filter(ProviderHealthEvent.provider == "tiingo")
            .all()
        )
        assert len(events) == 1
        assert events[0].status == ProviderHealthStatus.CONNECTED

    def test_failed_check_is_reflected_on_the_next_dashboard_read(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            provider_settings_router,
            "test_connection",
            lambda settings, provider, domain, db=None: (
                ProviderHealthStatus.RATE_LIMITED,
                "429 too many requests",
            ),
        )
        test_response = client.post("/api/v1/settings/providers/price_history/tiingo/test")
        assert test_response.status_code == 200
        assert test_response.json()["status"] == "rate_limited"

        dashboard = client.get("/api/v1/settings/providers").json()
        price_history = next(d for d in dashboard["domains"] if d["domain"] == "price_history")
        tiingo = next(p for p in price_history["providers"] if p["provider"] == "tiingo")
        assert tiingo["last_error_status"] == "rate_limited"
        assert tiingo["last_error_detail"] == "429 too many requests"
