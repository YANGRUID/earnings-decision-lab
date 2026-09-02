"""IBKR TWS Migration -- GET /internal/ibkr/tws-production-sanity, and the
ENABLE_INTERNAL_DIAGNOSTICS gate that post-cutover cleanup put in front of
it.

Deliberately does NOT attempt the happy path against a real IB Gateway:
that endpoint's whole purpose is to prove the *live, lifespan-owned*
shared connection works, which is a live-operations check, not something
a unit test can honestly simulate (a mocked "connected" result would
prove nothing about production and would be exactly the kind of
fabricated success this project's own test conventions reject elsewhere).

What IS worth pinning here is the structural contract:
  * with the default production config, the route does not exist at all;
  * with diagnostics explicitly enabled, it exists and refuses honestly
    when this process has no shared TWS provider (the real state whenever
    ibkr_provider != "tws");
  * its response schema structurally cannot carry a credential.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

SANITY_URL = "/api/v1/internal/ibkr/tws-production-sanity"


def _client_with(monkeypatch, **setting_overrides) -> Iterator[TestClient]:
    """Builds the app fresh under a specific Settings, so the router
    registration guard in api/main.py::create_app is genuinely exercised
    -- not merely the handler behind it."""
    from core.config import Settings, get_settings

    base = get_settings().model_dump()
    base.update(setting_overrides)
    overridden = Settings(**base)

    monkeypatch.setattr("api.main.get_settings", lambda: overridden)
    from api.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def diagnostics_disabled_client(monkeypatch) -> Iterator[TestClient]:
    yield from _client_with(monkeypatch, enable_internal_diagnostics=False)


@pytest.fixture
def diagnostics_enabled_client(monkeypatch) -> Iterator[TestClient]:
    yield from _client_with(monkeypatch, enable_internal_diagnostics=True)


class TestInternalDiagnosticsGate:
    def test_default_production_config_does_not_expose_the_endpoint(
        self, diagnostics_disabled_client
    ):
        """Post-cutover cleanup A2: default config must not carry this on
        the normal API surface at all."""
        assert diagnostics_disabled_client.get(SANITY_URL).status_code == 404

    def test_default_is_false_so_no_deployment_gets_it_by_accident(self):
        """The gate has to default closed -- an env-less deployment must
        not inherit the diagnostic surface."""
        from core.config import Settings

        assert Settings(_env_file=None).enable_internal_diagnostics is False

    def test_disabled_endpoint_is_absent_from_the_openapi_schema(
        self, diagnostics_disabled_client
    ):
        """Not merely 404 at call time -- genuinely not documented."""
        paths = diagnostics_disabled_client.get("/openapi.json").json()["paths"]
        assert not any("tws-production-sanity" in p for p in paths)

    def test_explicit_diagnostic_config_exposes_the_endpoint(self, diagnostics_enabled_client):
        """With the flag explicitly on, the route exists. It still refuses
        honestly here (422, not 500) because tests/conftest.py pins
        IBKR_PROVIDER=web, so no shared TWS provider is ever built -- the
        same real state as any Web-transport deployment."""
        response = diagnostics_enabled_client.get(SANITY_URL)
        assert response.status_code != 404
        # 422, not 400 -- this project's own InvalidRequestError handler
        # (api/exceptions.py) maps every domain-level validation refusal
        # to 422, and this endpoint reuses that existing convention.
        assert response.status_code == 422
        assert "not 'tws'" in response.text

    def test_enabled_endpoint_never_500s_without_a_provider(self, diagnostics_enabled_client):
        assert diagnostics_enabled_client.get(SANITY_URL).status_code != 500


class TestTwsProductionSanityResponseShape:
    def test_response_schema_exposes_no_credential_or_account_field(self):
        """Structural, mirrors test_services_system_status.py's own
        TwsStatus prohibition test: this response has no field capable of
        carrying an account id, username, or session secret at all."""
        from schemas.api import TwsProductionSanityResponse

        field_names = set(TwsProductionSanityResponse.model_fields)
        forbidden = {"account", "username", "password", "token", "session", "credential"}
        assert not any(word in name for name in field_names for word in forbidden)

    def test_router_exposes_no_write_or_order_path(self):
        """Read-only by construction: the diagnostic router has exactly
        one route and it is a GET."""
        from api.routers import tws_diagnostics

        methods = {m for route in tws_diagnostics.router.routes for m in route.methods}
        assert methods == {"GET"}
