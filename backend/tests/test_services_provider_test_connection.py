import re

import httpx
import pytest

from core.config import Settings
from models.enums import ProviderHealthStatus
from services.provider_test_connection import UnknownTestConnectionTargetError
from services.provider_test_connection import test_connection as run_test_connection

TIINGO_URL = re.compile(r"https://api\.tiingo\.com/tiingo/daily/aapl/prices.*")
ALPHA_VANTAGE_URL = re.compile(r"https://www\.alphavantage\.co/query.*")
SEC_EDGAR_URL = re.compile(r"https://data\.sec\.gov/api/xbrl/companyfacts/.*")


def _settings(**overrides) -> Settings:
    defaults = dict(
        tiingo_api_key="test-tiingo-key",
        alpha_vantage_api_key="test-av-key",
        _env_file=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestDispatcher:
    def test_raises_for_an_unknown_provider_domain_pair(self):
        with pytest.raises(UnknownTestConnectionTargetError):
            run_test_connection(_settings(), "made_up_provider", "price_history")

    def test_raises_for_a_real_provider_in_the_wrong_domain(self):
        # sec_edgar is real, but only for the filings domain.
        with pytest.raises(UnknownTestConnectionTargetError):
            run_test_connection(_settings(), "sec_edgar", "price_history")


class TestTiingo:
    def test_missing_api_key_is_auth_failed_without_any_network_call(self, httpx_mock):
        status, detail = run_test_connection(
            _settings(tiingo_api_key=None), "tiingo", "price_history"
        )
        assert status == ProviderHealthStatus.AUTH_FAILED
        assert "not configured" in (detail or "")
        # No request was ever registered/mocked -- if the code tried to
        # call out anyway, pytest-httpx would fail this test on teardown.

    def test_success_maps_to_connected(self, httpx_mock):
        httpx_mock.add_response(
            url=TIINGO_URL,
            json=[
                {
                    "date": "2026-03-01T00:00:00.000Z",
                    "close": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "open": 100.5,
                    "volume": 1000,
                }
            ],
        )
        status, detail = run_test_connection(_settings(), "tiingo", "price_history")
        assert status == ProviderHealthStatus.CONNECTED
        assert detail is None

    def test_401_maps_to_auth_failed(self, httpx_mock):
        httpx_mock.add_response(url=TIINGO_URL, status_code=401, json={"detail": "bad token"})
        status, _detail = run_test_connection(_settings(), "tiingo", "price_history")
        assert status == ProviderHealthStatus.AUTH_FAILED

    def test_429_maps_to_rate_limited(self, httpx_mock):
        # 429 is retried by TiingoMarketDataProvider's own tenacity policy
        # (see providers/tiingo.py::_retryable) -- stop_after_attempt(4)
        # means up to 4 real requests before the error is reraised; the
        # autouse _no_real_sleeps fixture (conftest.py) keeps the retry
        # backoff from actually waiting.
        for _ in range(4):
            httpx_mock.add_response(url=TIINGO_URL, status_code=429, json={"detail": "slow down"})
        status, _detail = run_test_connection(_settings(), "tiingo", "price_history")
        assert status == ProviderHealthStatus.RATE_LIMITED

    def test_500_maps_to_unavailable(self, httpx_mock):
        for _ in range(4):
            httpx_mock.add_response(url=TIINGO_URL, status_code=500, text="internal error")
        status, _detail = run_test_connection(_settings(), "tiingo", "price_history")
        assert status == ProviderHealthStatus.UNAVAILABLE

    def test_connection_refused_maps_to_unavailable(self, httpx_mock):
        for _ in range(4):
            httpx_mock.add_exception(httpx.ConnectError("connection refused"))
        status, _detail = run_test_connection(_settings(), "tiingo", "price_history")
        assert status == ProviderHealthStatus.UNAVAILABLE


class TestAlphaVantagePrices:
    def test_missing_api_key_is_auth_failed(self, httpx_mock):
        status, _detail = run_test_connection(
            _settings(alpha_vantage_api_key=None), "alpha_vantage", "price_history"
        )
        assert status == ProviderHealthStatus.AUTH_FAILED

    def test_rate_limit_note_maps_to_rate_limited(self, httpx_mock):
        httpx_mock.add_response(
            url=ALPHA_VANTAGE_URL,
            json={
                "Note": "Thank you for using Alpha Vantage! Our standard API rate limit is "
                "25 requests per day. Please visit https://... 5 calls per minute."
            },
        )
        status, _detail = run_test_connection(_settings(), "alpha_vantage", "price_history")
        assert status == ProviderHealthStatus.RATE_LIMITED


class TestAlphaVantageOptions:
    def test_premium_message_maps_to_premium_required(self, httpx_mock):
        httpx_mock.add_response(
            url=ALPHA_VANTAGE_URL,
            json={"message": "This is a premium endpoint on your current plan."},
        )
        status, detail = run_test_connection(_settings(), "alpha_vantage", "options")
        assert status == ProviderHealthStatus.PREMIUM_REQUIRED
        assert "premium" in (detail or "").lower()


class TestSecEdgar:
    def test_success_maps_to_connected(self, httpx_mock):
        httpx_mock.add_response(
            url=SEC_EDGAR_URL,
            json={"cik": 320193, "entityName": "Apple Inc.", "facts": {}},
        )
        status, _detail = run_test_connection(_settings(), "sec_edgar", "filings")
        assert status == ProviderHealthStatus.CONNECTED

    def test_404_maps_to_unavailable(self, httpx_mock):
        httpx_mock.add_response(url=SEC_EDGAR_URL, status_code=404, text="not found")
        status, _detail = run_test_connection(_settings(), "sec_edgar", "filings")
        assert status == ProviderHealthStatus.UNAVAILABLE


class TestIbkr:
    def test_gateway_unreachable_maps_to_gateway_offline(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))
        status, _detail = run_test_connection(_settings(), "ibkr", "options")
        assert status == ProviderHealthStatus.GATEWAY_OFFLINE

    def test_not_authenticated_session_maps_to_auth_failed(self, httpx_mock):
        httpx_mock.add_response(
            json={
                "authenticated": False,
                "established": False,
                "competing": False,
                "connected": False,
            }
        )
        status, _detail = run_test_connection(_settings(), "ibkr", "options")
        assert status == ProviderHealthStatus.AUTH_FAILED


class TestIbkrTws:
    """IBKR TWS Migration, Phase 3 readiness (Section 31/42) -- a real gap
    this task's frontend audit surfaced: _test_ibkr always tested the Web
    Gateway, even with ibkr_provider="tws" configured, so Data Providers'
    generic "Test Connection" button silently tested the wrong transport.
    Monkeypatches get_tws_status (imported by name into
    services.provider_test_connection) rather than touching httpx --
    TWS is a socket API, not HTTP, so httpx_mock has nothing to intercept
    here; this exercises only _test_ibkr's own status_label -> ProviderHealthStatus mapping."""

    def _tws_status(self, **overrides):
        from services.system_status import TwsStatus

        defaults = dict(
            configured=True,
            gateway_reachable=True,
            socket_connected=True,
            api_ready=True,
            market_data_quality="delayed",
            error=None,
            status_label="CONNECTED",
            last_heartbeat=None,
            reconnect_state="ready",
        )
        defaults.update(overrides)
        return TwsStatus(**defaults)

    def test_connected_maps_to_connected(self, monkeypatch):
        monkeypatch.setattr(
            "services.provider_test_connection.get_tws_status",
            lambda settings, probe=None: self._tws_status(),
        )
        status, detail = run_test_connection(
            _settings(ibkr_provider="tws"), "ibkr", "options"
        )
        assert status == ProviderHealthStatus.CONNECTED
        assert detail is None

    def test_gateway_unreachable_maps_to_gateway_offline(self, monkeypatch):
        monkeypatch.setattr(
            "services.provider_test_connection.get_tws_status",
            lambda settings, probe=None: self._tws_status(
                gateway_reachable=False,
                socket_connected=False,
                api_ready=False,
                status_label="GATEWAY_UNREACHABLE",
                error="could not reach IB Gateway/TWS",
                reconnect_state="failed",
            ),
        )
        status, detail = run_test_connection(
            _settings(ibkr_provider="tws"), "ibkr", "options"
        )
        assert status == ProviderHealthStatus.GATEWAY_OFFLINE
        assert detail == "could not reach IB Gateway/TWS"

    def test_not_ready_maps_to_auth_failed(self, monkeypatch):
        monkeypatch.setattr(
            "services.provider_test_connection.get_tws_status",
            lambda settings, probe=None: self._tws_status(
                api_ready=False,
                status_label="AUTH_REQUIRED",
                error="no nextValidId arrived",
                reconnect_state="connected",
            ),
        )
        status, detail = run_test_connection(
            _settings(ibkr_provider="tws"), "ibkr", "options"
        )
        assert status == ProviderHealthStatus.AUTH_FAILED
        assert detail == "no nextValidId arrived"

    def test_web_config_never_calls_get_tws_status(self, monkeypatch, httpx_mock):
        """The default ibkr_provider="web" path must be byte-for-byte
        unaffected -- confirmed by making get_tws_status raise if it's
        ever even called."""

        def _boom(*args, **kwargs):
            raise AssertionError("get_tws_status must not be called when ibkr_provider=web")

        monkeypatch.setattr("services.provider_test_connection.get_tws_status", _boom)
        httpx_mock.add_response(
            json={"authenticated": True, "established": True, "competing": False, "connected": True}
        )
        status, _detail = run_test_connection(_settings(), "ibkr", "options")
        assert status == ProviderHealthStatus.CONNECTED
