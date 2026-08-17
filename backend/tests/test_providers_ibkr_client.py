import re

import httpx
import pytest

from providers.ibkr_client import (
    IBKRClient,
    IBKRCompetingSessionError,
    IBKRError,
    IBKRGatewayUnavailableError,
    IBKRNotAuthenticatedError,
    IBKRRateLimitedError,
    decode_market_data_quality,
)

BASE_URL = "https://localhost:5001/v1/api"
AUTH_STATUS_URL = re.compile(r"https://localhost:5001/v1/api/iserver/auth/status.*")

# The real response shape captured live during Phase 13 verification
# against an authenticated Gateway.
REAL_AUTHENTICATED_STATUS = {
    "authenticated": True,
    "established": True,
    "competing": False,
    "connected": True,
    "message": "",
    "MAC": "00:00:00:00:00:00",
    "serverInfo": {"serverName": "test", "serverVersion": "test"},
    "fail": "",
}


class TestDecodeMarketDataQuality:
    def test_real_time_flag(self):
        assert decode_market_data_quality("RpB") == "live"

    def test_delayed_flag_from_real_nvda_stock_response(self):
        # Real field 6509 value captured live for NVDA underlying stock.
        assert decode_market_data_quality("DB") == "delayed"

    def test_frozen_flag_from_real_nvda_option_response(self):
        # Real field 6509 value captured live for NVDA option contracts,
        # ~2 minutes after the regular session close.
        assert decode_market_data_quality("ZBd") == "frozen"

    def test_frozen_delayed_flag_maps_to_frozen(self):
        assert decode_market_data_quality("Y") == "frozen"

    def test_not_subscribed_flag(self):
        assert decode_market_data_quality("N") == "unavailable"

    def test_unrecognized_flag_is_unknown_not_guessed(self):
        assert decode_market_data_quality("Q") == "unknown"

    def test_missing_flag_is_unknown(self):
        assert decode_market_data_quality(None) == "unknown"
        assert decode_market_data_quality("") == "unknown"


class TestIBKRClientGet:
    def test_get_raises_gateway_unavailable_on_connect_error(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))
        client = IBKRClient(base_url=BASE_URL)

        with pytest.raises(IBKRGatewayUnavailableError):
            client.get("/iserver/auth/status")

    def test_get_raises_gateway_unavailable_on_timeout(self, httpx_mock):
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))
        client = IBKRClient(base_url=BASE_URL)

        with pytest.raises(IBKRGatewayUnavailableError):
            client.get("/iserver/auth/status")

    def test_get_raises_rate_limited_on_429(self, httpx_mock):
        httpx_mock.add_response(status_code=429, json={"error": "rate limited"})
        client = IBKRClient(base_url=BASE_URL)

        with pytest.raises(IBKRRateLimitedError):
            client.get("/iserver/auth/status")

    def test_get_raises_ibkr_error_on_other_http_error(self, httpx_mock):
        httpx_mock.add_response(status_code=500, text="internal error")
        client = IBKRClient(base_url=BASE_URL)

        with pytest.raises(IBKRError):
            client.get("/iserver/auth/status")

    def test_get_returns_response_on_success(self, httpx_mock):
        httpx_mock.add_response(json={"ok": True})
        client = IBKRClient(base_url=BASE_URL)

        response = client.get("/iserver/auth/status")

        assert response.json() == {"ok": True}


class TestAuthStatus:
    def test_parses_real_authenticated_response(self, httpx_mock):
        httpx_mock.add_response(url=AUTH_STATUS_URL, json=REAL_AUTHENTICATED_STATUS)
        client = IBKRClient(base_url=BASE_URL)

        status = client.auth_status()

        assert status.authenticated is True
        assert status.connected is True
        assert status.competing is False

    def test_parses_not_authenticated_response(self, httpx_mock):
        httpx_mock.add_response(
            url=AUTH_STATUS_URL,
            json={"authenticated": False, "connected": False, "competing": False},
        )
        client = IBKRClient(base_url=BASE_URL)

        status = client.auth_status()

        assert status.authenticated is False


class TestEnsureAuthenticated:
    def test_returns_status_when_fully_authenticated(self, httpx_mock):
        httpx_mock.add_response(url=AUTH_STATUS_URL, json=REAL_AUTHENTICATED_STATUS)
        client = IBKRClient(base_url=BASE_URL)

        status = client.ensure_authenticated()

        assert status.authenticated is True

    def test_raises_not_authenticated_when_session_not_established(self, httpx_mock):
        httpx_mock.add_response(
            url=AUTH_STATUS_URL,
            json={"authenticated": False, "connected": False, "competing": False},
        )
        client = IBKRClient(base_url=BASE_URL)

        with pytest.raises(IBKRNotAuthenticatedError):
            client.ensure_authenticated()

    def test_raises_competing_session_error(self, httpx_mock):
        httpx_mock.add_response(
            url=AUTH_STATUS_URL,
            json={"authenticated": True, "connected": True, "competing": True},
        )
        client = IBKRClient(base_url=BASE_URL)

        with pytest.raises(IBKRCompetingSessionError):
            client.ensure_authenticated()

    def test_competing_session_takes_priority_over_authenticated_flags(self, httpx_mock):
        # A competing session is reported even when authenticated=True --
        # must not be masked by the "authenticated" check.
        httpx_mock.add_response(
            url=AUTH_STATUS_URL,
            json={"authenticated": True, "connected": True, "competing": True},
        )
        client = IBKRClient(base_url=BASE_URL)

        with pytest.raises(IBKRCompetingSessionError):
            client.ensure_authenticated()
