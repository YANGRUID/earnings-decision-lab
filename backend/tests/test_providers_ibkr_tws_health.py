"""IBKR TWS Migration Phase 2, Section 11 -- TwsHealthProbe unit tests.

Proves the actual fix: once READY, repeated snapshot() calls read the
held connection's state directly (no new socket); a failed connection is
retried only after the backoff floor, never on every single call.
"""

from unittest.mock import MagicMock

from providers.ibkr_client import IBKRGatewayUnavailableError
from providers.ibkr_tws_client import TWSConnectionState
from providers.ibkr_tws_health import TwsHealthProbe


def _probe(**kwargs) -> TwsHealthProbe:
    defaults = {"host": "host.docker.internal", "port": 4002, "client_id": 999}
    defaults.update(kwargs)
    return TwsHealthProbe(**defaults)


class TestTwsHealthProbe:
    def test_first_call_creates_and_connects(self, monkeypatch):
        connect_calls = []
        monkeypatch.setattr(
            "providers.ibkr_tws_health.TWSConnectionManager.connect_and_start",
            lambda self: (
                connect_calls.append(1) or setattr(self, "_state", TWSConnectionState.READY)
            ),
        )
        probe = _probe()
        snapshot = probe.snapshot()
        assert len(connect_calls) == 1
        assert snapshot.provider == "tws"

    def test_second_call_reuses_the_ready_connection_without_reconnecting(self, monkeypatch):
        """The actual fix under test: once READY, snapshot() must not
        touch connect_and_start() again -- a pure attribute read."""
        connect_calls = []
        monkeypatch.setattr(
            "providers.ibkr_tws_health.TWSConnectionManager.connect_and_start",
            lambda self: (
                connect_calls.append(1) or setattr(self, "_state", TWSConnectionState.READY)
            ),
        )
        probe = _probe()
        probe.snapshot()
        probe.snapshot()
        probe.snapshot()
        assert len(connect_calls) == 1  # not 3

    def test_holds_the_same_connection_instance_across_calls(self, monkeypatch):
        monkeypatch.setattr(
            "providers.ibkr_tws_health.TWSConnectionManager.connect_and_start",
            lambda self: setattr(self, "_state", TWSConnectionState.READY),
        )
        probe = _probe()
        probe.snapshot()
        first_connection = probe._connection  # noqa: SLF001
        probe.snapshot()
        assert probe._connection is first_connection  # noqa: SLF001

    def test_failed_connection_is_not_retried_before_the_backoff_floor(self, monkeypatch):
        attempts = []

        def _fail(self):
            attempts.append(1)
            raise IBKRGatewayUnavailableError("still down")

        monkeypatch.setattr(
            "providers.ibkr_tws_health.TWSConnectionManager.connect_and_start", _fail
        )
        probe = _probe(min_retry_interval_seconds=9999)  # never elapses during this test
        probe.snapshot()
        probe.snapshot()
        probe.snapshot()
        assert len(attempts) == 1  # not hammered on every call

    def test_retries_after_the_backoff_floor_elapses(self, monkeypatch):
        attempts = []

        def _fail(self):
            attempts.append(1)
            raise IBKRGatewayUnavailableError("still down")

        monkeypatch.setattr(
            "providers.ibkr_tws_health.TWSConnectionManager.connect_and_start", _fail
        )
        probe = _probe(min_retry_interval_seconds=0.0)  # always elapsed
        probe.snapshot()
        probe.snapshot()
        assert len(attempts) == 2

    def test_snapshot_reports_honest_failure_without_raising(self, monkeypatch):
        def _fail(self):
            raise IBKRGatewayUnavailableError("connection refused")

        monkeypatch.setattr(
            "providers.ibkr_tws_health.TWSConnectionManager.connect_and_start", _fail
        )
        probe = _probe()
        snapshot = probe.snapshot()  # must not raise
        assert snapshot.api_ready is False

    def test_shutdown_closes_and_clears_the_held_connection(self, monkeypatch):
        monkeypatch.setattr(
            "providers.ibkr_tws_health.TWSConnectionManager.connect_and_start",
            lambda self: setattr(self, "_state", TWSConnectionState.READY),
        )
        shutdown_calls = []
        monkeypatch.setattr(
            "providers.ibkr_tws_health.TWSConnectionManager.shutdown",
            lambda self: shutdown_calls.append(1),
        )
        probe = _probe()
        probe.snapshot()
        probe.shutdown()
        assert shutdown_calls == [1]
        assert probe._connection is None  # noqa: SLF001

    def test_shutdown_before_any_connection_is_a_noop(self):
        probe = _probe()
        probe.shutdown()  # must not raise

    def test_uses_a_distinct_client_id_from_the_data_serving_connection(self):
        """Section 9 -- the health probe must never share identity with
        whatever connection an IBKRTWSProvider uses to serve real
        market data."""
        probe = _probe(client_id=555)
        assert probe._client_id == 555  # noqa: SLF001


class TestNoOrderOperationsOnHealthProbe:
    def test_probe_exposes_no_order_method(self):
        """Same Section 0/46 guarantee as the rest of this migration --
        the health probe's own public surface is just snapshot/shutdown,
        never an order-capable method."""
        public_methods = {name for name in dir(TwsHealthProbe) if not name.startswith("_")}
        assert public_methods == {"snapshot", "shutdown"}


class TestReuseWithMagicMockConnection:
    """A lighter-weight alternative to monkeypatching the real class --
    confirms the probe's own call pattern against a bare mock."""

    def test_connect_and_start_called_once_then_health_snapshot_reused(self, monkeypatch):
        fake_connection = MagicMock()
        fake_connection.state = TWSConnectionState.READY
        monkeypatch.setattr(
            "providers.ibkr_tws_health.TWSConnectionManager",
            lambda **kwargs: fake_connection,
        )
        probe = _probe()
        probe.snapshot()
        probe.snapshot()
        assert fake_connection.connect_and_start.call_count == 1
        assert fake_connection.health_snapshot.call_count == 2
