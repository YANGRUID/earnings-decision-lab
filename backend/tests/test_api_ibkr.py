"""Phase 4.8A -- GET /ibkr/connect (the browser-facing Gateway login URL)
and a full-HTTP-stack confirmation that GET /system-status surfaces the
real IBKR status_label correctly. The underlying auth-status mapping
logic itself is already exhaustively covered at the service layer in
test_services_system_status.py -- this file's job is confirming the
router/response_model layer doesn't lose or mangle it, and that the new
/ibkr/connect endpoint does exactly what it claims: hand back a URL,
nothing else.
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from core.config import Settings


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


class TestIbkrConnectEndpoint:
    def test_returns_the_default_gateway_url(self, client):
        response = client.get("/api/v1/ibkr/connect")

        assert response.status_code == 200
        assert response.json() == {"url": "https://localhost:5000"}

    def test_reflects_a_configured_gateway_port(self, client, monkeypatch):
        import api.routers.ibkr as ibkr_module

        monkeypatch.setattr(
            ibkr_module, "get_settings", lambda: Settings(ibkr_gateway_port=6100, _env_file=None)
        )

        response = client.get("/api/v1/ibkr/connect")

        assert response.json() == {"url": "https://localhost:6100"}

    def test_response_contains_only_a_url_never_credentials_or_a_session(self, client):
        # Explicit, deliberate regression guard matching this endpoint's
        # own docstring: it hands back a URL and nothing else -- no
        # password field, no token, no account identifier.
        response = client.get("/api/v1/ibkr/connect")

        assert set(response.json().keys()) == {"url"}

    def test_uses_get_not_post(self, client):
        # This endpoint never accepts a body (no credentials to submit)
        # -- POST must not be a valid alternative route to it.
        response = client.post("/api/v1/ibkr/connect")

        assert response.status_code == 405


class TestSystemStatusIbkrField:
    """Full HTTP-stack coverage for the three states the runtime-
    automation brief asks the status page to distinguish."""

    def _use_ibkr_base_url(self, monkeypatch) -> None:
        import api.routers.system_status as status_module

        monkeypatch.setattr(
            status_module,
            "get_settings",
            lambda: Settings(ibkr_base_url="https://localhost:5001/v1/api", _env_file=None),
        )

    def test_connected(self, client, monkeypatch, httpx_mock):
        self._use_ibkr_base_url(monkeypatch)
        httpx_mock.add_response(
            url="https://localhost:5001/v1/api/iserver/auth/status",
            json={"authenticated": True, "connected": True, "competing": False},
        )

        response = client.get("/api/v1/system-status")

        assert response.status_code == 200
        assert response.json()["ibkr"]["status_label"] == "CONNECTED"

    def test_authentication_required(self, client, monkeypatch, httpx_mock):
        self._use_ibkr_base_url(monkeypatch)
        httpx_mock.add_response(
            url="https://localhost:5001/v1/api/iserver/auth/status",
            json={"authenticated": False, "connected": False, "competing": False},
        )

        response = client.get("/api/v1/system-status")

        assert response.json()["ibkr"]["status_label"] == "AUTH_REQUIRED"

    def test_gateway_unavailable(self, client, monkeypatch, httpx_mock):
        """Reconnect/offline scenario: the Gateway is down entirely --
        the full request must still succeed (200), never surface as a
        crashed /system-status call just because IBKR is unreachable."""
        self._use_ibkr_base_url(monkeypatch)
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))

        response = client.get("/api/v1/system-status")

        assert response.status_code == 200
        assert response.json()["ibkr"]["status_label"] == "GATEWAY_UNREACHABLE"
        assert response.json()["ibkr"]["gateway_reachable"] is False
