"""Test-connection for the EarningsAPI calendar provider (2026-09-02).

Regression: the Settings "Test Connection" button reported
"no test-connection check exists for provider='earningsapi'" even while
the scheduled sync was succeeding with the same key.
"""

import httpx
import pytest

from core.config import Settings
from models.enums import ProviderHealthStatus
from services.provider_test_connection import UnknownTestConnectionTargetError
from services.provider_test_connection import test_connection as run_test_connection


def _settings(key: str | None) -> Settings:
    return Settings(earningsapi_api_key=key or "")


class TestEarningsApiTestConnection:
    def test_is_now_a_known_target(self, monkeypatch):
        monkeypatch.setattr(
            "services.provider_test_connection.EarningsApiCalendarProvider.get_earnings_calendar",
            lambda self, a, b: [],
        )
        status, detail = run_test_connection(_settings("k"), "earningsapi", "earnings_calendar")
        assert status == ProviderHealthStatus.CONNECTED
        assert detail is None

    def test_missing_key_is_auth_failed_not_unknown(self, monkeypatch):
        monkeypatch.setattr(
            "services.provider_test_connection.resolve_secret", lambda *a, **k: None
        )
        status, detail = run_test_connection(_settings(None), "earningsapi", "earnings_calendar")
        assert status == ProviderHealthStatus.AUTH_FAILED
        assert "EARNINGSAPI_API_KEY" in (detail or "")

    def test_http_401_maps_to_auth_failed(self, monkeypatch):
        def boom(self, a, b):
            req = httpx.Request("GET", "https://api.earningsapi.com/v1/x")
            response = httpx.Response(401, request=req)
            raise httpx.HTTPStatusError("401", request=req, response=response)

        target = "services.provider_test_connection.EarningsApiCalendarProvider"
        monkeypatch.setattr(f"{target}.get_earnings_calendar", boom)
        status, _ = run_test_connection(_settings("k"), "earningsapi", "earnings_calendar")
        assert status == ProviderHealthStatus.AUTH_FAILED

    def test_unknown_pairs_still_raise(self):
        with pytest.raises(UnknownTestConnectionTargetError):
            run_test_connection(_settings("k"), "earningsapi", "options")
