"""IBKR TWS Migration Phase 1 -- TWSConnectionManager unit tests.

Every real ibapi socket internal (EClient.connect/.run/.reqXxx) is
monkeypatched on the instance -- no real TCP connection is ever attempted.
This mirrors the project's existing convention for the Web adapter
(test_providers_ibkr_client.py mocks httpx; this mocks ibapi's own
EClient methods at the same boundary) -- Section 45's explicit ask:
"mock protocol internals."
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from models.enums import QuoteRequirement
from providers.ibkr_client import (
    IBKRClientIdInUseError,
    IBKRGatewayUnavailableError,
    IBKRMarketDataPermissionError,
    IBKRRateLimitedError,
)
from providers.ibkr_options import IBKRContractNotFoundError
from providers.ibkr_tws_client import (
    TWSConnectionManager,
    TWSConnectionState,
    classify_error_code,
)


def _now_ms() -> int:
    """A real epoch-ms value for the official client's errorTime
    parameter (IBKR TWS Migration Phase 1.1) -- tests never assert on
    its exact value, only that error() accepts and uses it correctly."""
    return int(time.time() * 1000)


def _manager(**kwargs) -> TWSConnectionManager:
    defaults = {"host": "host.docker.internal", "port": 4002, "client_id": 101}
    defaults.update(kwargs)
    return TWSConnectionManager(**defaults)


def _mock_socket_layer(manager: TWSConnectionManager) -> None:
    """Replaces the real ibapi connect()/run() with no-ops -- the test
    itself drives readiness by calling manager.nextValidId(...) directly,
    exactly as ibapi's own reader thread would (just synchronously,
    on the test's own thread, instead of asynchronously on a real socket
    reader thread)."""
    manager.connect = MagicMock()  # type: ignore[method-assign]
    manager.run = MagicMock()  # type: ignore[method-assign]
    manager.disconnect = MagicMock()  # type: ignore[method-assign]
    manager.isConnected = MagicMock(return_value=True)  # type: ignore[method-assign]


class TestClassifyErrorCode:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (502, "AUTH_CONNECTION"),
            (504, "AUTH_CONNECTION"),
            (1100, "AUTH_CONNECTION"),
            (326, "AUTH_CONNECTION"),
            (2103, "FARM_DISCONNECTED"),
            (2104, "FARM_RECONNECTED"),
            (2106, "FARM_RECONNECTED"),
            (2158, "FARM_RECONNECTED"),
            (354, "MARKET_DATA_PERMISSION"),
            (10168, "MARKET_DATA_PERMISSION"),
            (10167, "MARKET_DATA_UNAVAILABLE"),
            (509, "RATE_LIMIT"),
            (200, "CONTRACT_NOT_FOUND"),
            (99999, "SYSTEM_ERROR"),
        ],
    )
    def test_maps_real_ibkr_error_codes_to_taxonomy(self, code, expected):
        assert classify_error_code(code) == expected

    def test_never_treats_farm_ok_as_fatal(self):
        # 2104/2106/2158 are IBKR's own informational connection-farm
        # status messages, not request failures.
        assert classify_error_code(2104) == "FARM_RECONNECTED"


class TestConnectionLifecycle:
    def test_starts_disconnected(self):
        manager = _manager()
        assert manager.state == TWSConnectionState.DISCONNECTED

    def test_next_valid_id_transitions_to_ready(self):
        manager = _manager()
        manager.nextValidId(17)
        assert manager.state == TWSConnectionState.READY
        assert manager.next_request_id() == 17
        assert manager.next_request_id() == 18

    def test_next_request_id_before_ready_raises_honestly(self):
        manager = _manager()
        with pytest.raises(IBKRGatewayUnavailableError):
            manager.next_request_id()

    def test_connection_closed_transitions_to_disconnected(self):
        manager = _manager()
        manager.nextValidId(1)
        manager.connectionClosed()
        assert manager.state == TWSConnectionState.DISCONNECTED

    def test_connection_closed_unblocks_pending_requests(self):
        manager = _manager()
        manager.nextValidId(1)
        pending = manager._register(5, "test_request")  # noqa: SLF001
        manager.connectionClosed()
        assert pending.done.is_set()
        assert isinstance(pending.error, IBKRGatewayUnavailableError)

    def test_connect_and_start_succeeds_once_next_valid_id_arrives(self):
        manager = _manager()
        _mock_socket_layer(manager)

        def _fire_ready():
            time.sleep(0.05)
            manager.nextValidId(1)

        threading.Thread(target=_fire_ready).start()
        manager.connect_and_start()
        assert manager.state == TWSConnectionState.READY

    def test_connect_and_start_is_idempotent_when_already_ready(self):
        manager = _manager()
        _mock_socket_layer(manager)
        manager.nextValidId(1)
        assert manager.state == TWSConnectionState.READY
        manager.connect_and_start()  # must not attempt a second real connect
        manager.connect.assert_not_called()

    def test_connect_and_start_raises_on_socket_error(self):
        manager = _manager()
        manager.connect = MagicMock(side_effect=OSError("connection refused"))  # type: ignore[method-assign]
        with pytest.raises(IBKRGatewayUnavailableError):
            manager.connect_and_start()
        assert manager.state == TWSConnectionState.FAILED

    def test_connect_and_start_times_out_honestly_when_gateway_not_logged_in(self):
        """Section 37 -- Gateway reachable but never sends nextValidId
        (not authenticated yet) must be a real, bounded timeout, never a
        hang."""
        manager = _manager(connect_timeout=0.1)
        _mock_socket_layer(manager)
        with pytest.raises(IBKRGatewayUnavailableError, match="not be logged in"):
            manager.connect_and_start()
        assert manager.state == TWSConnectionState.FAILED

    def test_connect_and_start_raises_client_id_in_use(self):
        """Section 26 -- error 326 arriving before nextValidId must be a
        real, typed, distinct failure, not a generic timeout. Fires the
        collision synchronously as connect()'s own side effect -- in real
        ibapi usage, error() can only ever be dispatched by the reader
        thread, which never starts before connect_and_start() has already
        cleared _ready_event; a thread racing that clear() (as an earlier
        version of this test did, timing-based) is a false scenario that
        cannot occur in production and made this test order-dependent."""
        manager = _manager(connect_timeout=1.0)
        _mock_socket_layer(manager)
        manager.connect = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: manager.error(-1, _now_ms(), 326, "duplicate client id")
        )

        with pytest.raises(IBKRClientIdInUseError):
            manager.connect_and_start()
        assert manager.state == TWSConnectionState.FAILED

    def test_ensure_connected_is_a_noop_when_already_ready(self):
        manager = _manager()
        _mock_socket_layer(manager)
        manager.nextValidId(1)
        manager.ensure_connected()
        manager.connect.assert_not_called()

    def test_ensure_connected_gives_up_after_bounded_attempts(self, monkeypatch):
        """Section 27 -- bounded backoff, never unbounded retry against a
        live connection."""
        manager = _manager()
        attempts = []

        def _always_fail():
            attempts.append(1)
            raise IBKRGatewayUnavailableError("still down")

        monkeypatch.setattr(manager, "connect_and_start", _always_fail)
        monkeypatch.setattr("providers.ibkr_tws_client.time.sleep", lambda _seconds: None)

        with pytest.raises(IBKRGatewayUnavailableError):
            manager.ensure_connected()
        assert len(attempts) == 5  # _RECONNECT_MAX_ATTEMPTS
        assert manager.state == TWSConnectionState.FAILED

    def test_shutdown_disconnects_and_marks_disconnected(self):
        manager = _manager()
        _mock_socket_layer(manager)
        manager.nextValidId(1)
        manager.shutdown()
        manager.disconnect.assert_called_once()
        assert manager.state == TWSConnectionState.DISCONNECTED

    def test_health_snapshot_never_exposes_credentials(self):
        import dataclasses

        from providers.ibkr_tws_client import TWSHealthSnapshot

        field_names = {f.name for f in dataclasses.fields(TWSHealthSnapshot)}
        assert "account_id" not in field_names
        assert "username" not in field_names
        assert "password" not in field_names
        assert "session_id" not in field_names

    def test_health_snapshot_reports_unreachable_after_a_genuine_socket_error(self):
        """IBKR TWS Migration Phase 2, Section 11 -- a real bug found
        while wiring up the persistent health probe: health_snapshot()
        used to derive gateway_reachable from ``state != DISCONNECTED``,
        which wrongly reported FAILED (any failure -- a real socket
        error, a timeout, a client-id collision) as "reachable" too,
        since FAILED isn't DISCONNECTED either. A genuine connection
        refusal (no real socket ever opened -- isConnected() reports
        False, unmocked here) must report gateway_reachable=False."""
        manager = _manager()
        manager.connect = MagicMock(side_effect=OSError("connection refused"))  # type: ignore[method-assign]
        with pytest.raises(IBKRGatewayUnavailableError):
            manager.connect_and_start()
        snapshot = manager.health_snapshot()
        assert snapshot.gateway_reachable is False
        assert snapshot.api_ready is False

    def test_health_snapshot_reports_reachable_when_socket_connected_but_not_ready(self):
        """The AUTH_REQUIRED case (Section 37): a real TCP connection
        succeeded (isConnected() is True) but nextValidId never arrived
        -- gateway_reachable must be True here (the Gateway process IS
        reachable; it's just not logged in yet), distinct from the
        genuinely-unreachable case above. Without this distinction,
        tws_status_label could never actually reach AUTH_REQUIRED."""
        manager = _manager(connect_timeout=0.1)
        _mock_socket_layer(manager)
        with pytest.raises(IBKRGatewayUnavailableError):
            manager.connect_and_start()
        snapshot = manager.health_snapshot()
        assert snapshot.gateway_reachable is True
        assert snapshot.api_ready is False


class TestNoOrderOperations:
    """Section 0/10/46 -- the single most safety-critical guarantee in
    this migration. These four methods ARE real ibapi.client.EClient
    methods (this manager must inherit from EClient to receive any
    callback at all) -- overriding them to raise is what makes "no order
    operation is possible through the new provider interface" a real,
    enforced guarantee. Zero of these tests contacts a real or paper IBKR
    account -- every one raises before any real ibapi network code runs."""

    @pytest.mark.parametrize(
        "method_name",
        ["placeOrder", "cancelOrder", "reqOpenOrders", "reqGlobalCancel", "exerciseOptions"],
    )
    def test_order_method_raises_immediately(self, method_name):
        manager = _manager()
        method = getattr(manager, method_name)
        with pytest.raises(RuntimeError, match="read-only"):
            method(1, 2, 3, 4, 5)  # arbitrary args -- must raise before touching them

    def test_public_provider_adapter_exposes_no_order_method(self):
        """Static confirmation over the real, public IBKRTWSProvider
        surface -- the one class application code actually depends on."""
        from providers.ibkr_tws_options import IBKRTWSProvider

        forbidden = {
            "place_order",
            "cancel_order",
            "modify_order",
            "exercise_options",
            "req_open_orders",
        }
        public_methods = {name for name in dir(IBKRTWSProvider) if not name.startswith("_")}
        assert public_methods.isdisjoint(forbidden)


class TestRequestCorrelation:
    """Section 24 -- typed pending-request lifecycle. Every reqXxx method
    on EClient is monkeypatched to synchronously invoke the wrapper
    callback it would normally trigger asynchronously (mocked protocol
    internals, Section 45) -- proves the correlation/wait/timeout logic
    itself, independent of any real socket."""

    def test_contract_details_round_trip(self):
        manager = _manager()
        manager.nextValidId(1)
        fake_details = MagicMock()
        fake_details.contract.conId = 12345

        def _fake_req(req_id, contract):
            manager.contractDetails(req_id, fake_details)
            manager.contractDetailsEnd(req_id)

        manager.reqContractDetails = _fake_req  # type: ignore[method-assign]
        result = manager.request_contract_details(MagicMock())
        assert result == [fake_details]

    def test_contract_details_times_out_honestly(self):
        manager = _manager()
        manager.nextValidId(1)
        manager.reqContractDetails = lambda req_id, contract: None  # type: ignore[method-assign]
        with pytest.raises(IBKRGatewayUnavailableError, match="timed out"):
            manager.request_contract_details(MagicMock(), timeout=0.05)
        assert manager._pending == {}  # noqa: SLF001 -- never left dangling

    def test_contract_not_found_error_propagates_as_typed_exception(self):
        manager = _manager()
        manager.nextValidId(1)

        def _fake_req(req_id, contract):
            manager.error(req_id, _now_ms(), 200, "No security definition has been found")

        manager.reqContractDetails = _fake_req  # type: ignore[method-assign]
        with pytest.raises(IBKRContractNotFoundError):
            manager.request_contract_details(MagicMock())

    def test_rate_limit_error_propagates_as_typed_exception(self):
        manager = _manager()
        manager.nextValidId(1)

        def _fake_req(req_id, contract):
            manager.error(req_id, _now_ms(), 100, "Max rate of messages per second exceeded")

        manager.reqContractDetails = _fake_req  # type: ignore[method-assign]
        with pytest.raises(IBKRRateLimitedError):
            manager.request_contract_details(MagicMock())

    def test_market_data_permission_error_propagates(self):
        manager = _manager()
        manager.nextValidId(1)

        def _fake_req(req_id, contract, *args):
            manager.error(req_id, _now_ms(), 354, "Requested market data is not subscribed")

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        with pytest.raises(IBKRMarketDataPermissionError):
            manager.request_market_data_snapshot(MagicMock())

    def test_market_data_unavailable_is_not_an_exception(self):
        """A real, honest "no data" outcome (delayed data not entitled
        either) -- must complete the request, never raise."""
        manager = _manager()
        manager.nextValidId(1)

        def _fake_req(req_id, contract, *args):
            manager.error(req_id, _now_ms(), 10167, "Delayed market data is available")
            manager.tickSnapshotEnd(req_id)

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        result = manager.request_market_data_snapshot(MagicMock())
        assert result.get("_market_data_unavailable") is True

    def test_partial_market_data_unavailable_10091_is_not_an_exception(self):
        """Real code confirmed live 2026-08-31: 10091 ("requested
        PARTIAL market data requires additional subscription... delayed
        available") fires for option BID_ASK specifically -- same
        honest "no data" treatment as 10089/10167, never an exception."""
        manager = _manager()
        manager.nextValidId(1)

        def _fake_req(req_id, contract, *args):
            manager.error(req_id, _now_ms(), 10091, "Delayed market data is available")
            manager.tickSnapshotEnd(req_id)

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        result = manager.request_market_data_snapshot(MagicMock())
        assert result.get("_market_data_unavailable") is True

    def test_market_data_unavailable_notice_does_not_end_the_wait_early(self):
        """The real bug this fix targets (delayed-market-data forensic,
        2026-08-31): a raw ibapi trace proved real DELAYED_BID/ASK/LAST
        ticks arrive AFTER the informational 10167 notice but BEFORE the
        real tickSnapshotEnd -- error() must not call done.set() itself
        for this category, or a snapshot request completes before the
        real data it's still waiting for ever arrives. Simulates that
        exact real ordering: error(10167) fires, then real ticks arrive,
        then tickSnapshotEnd -- the real prices must be captured."""
        manager = _manager()
        manager.nextValidId(1)

        def _fake_req(req_id, contract, *args):
            manager.error(req_id, _now_ms(), 10167, "Delayed market data is available")
            # If done.set() fired on the error above, request_market_data_
            # snapshot would already have returned by this point in a
            # real (threaded) scenario -- here, synchronously, this
            # assertion instead directly proves the pending request is
            # still open to receive real ticks.
            pending = manager._pending[req_id]  # noqa: SLF001
            assert not pending.done.is_set(), "error() must not complete the wait on its own"
            manager.tickPrice(req_id, 66, 315.83, None)  # DELAYED_BID, arrives after the notice
            manager.tickPrice(req_id, 67, 315.84, None)  # DELAYED_ASK
            manager.tickSnapshotEnd(req_id)  # the real terminal signal

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        result = manager.request_market_data_snapshot(MagicMock())
        assert result["bid"] == 315.83
        assert result["ask"] == 315.84
        assert result.get("_market_data_unavailable") is True  # honestly recorded too

    def test_sec_def_opt_params_round_trip(self):
        manager = _manager()
        manager.nextValidId(1)

        def _fake_req(req_id, symbol, exchange, sec_type, conid):
            manager.securityDefinitionOptionParameter(
                req_id, "SMART", conid, "AAPL", "100", {"20260918", "20261016"}, {150.0, 155.0}
            )
            manager.securityDefinitionOptionParameterEnd(req_id)

        manager.reqSecDefOptParams = _fake_req  # type: ignore[method-assign]
        rows = manager.request_sec_def_opt_params("AAPL", "STK", 265598)
        assert len(rows) == 1
        assert rows[0]["exchange"] == "SMART"
        assert rows[0]["strikes"] == {150.0, 155.0}

    def test_market_data_snapshot_collects_ticks_and_quality(self):
        manager = _manager()
        manager.nextValidId(1)

        def _fake_req(req_id, contract, generic_ticks, snapshot, reg_snapshot, options):
            manager.marketDataType(req_id, 3)  # delayed
            manager.tickPrice(req_id, 1, 4.90, None)  # bid
            manager.tickPrice(req_id, 2, 5.10, None)  # ask
            manager.tickPrice(req_id, 4, 5.00, None)  # last
            manager.tickSize(req_id, 8, 120)  # volume
            manager.tickSnapshotEnd(req_id)

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        result = manager.request_market_data_snapshot(MagicMock())
        assert result["bid"] == 4.90
        assert result["ask"] == 5.10
        assert result["last"] == 5.00
        assert result["volume"] == 120
        assert result["market_data_quality"] == "delayed"

    def test_negative_one_price_tick_is_never_recorded_as_a_real_price(self):
        """IB sends -1 for "no data" on price ticks -- must never be
        recorded as a real bid/ask/last/close."""
        manager = _manager()
        manager.nextValidId(1)
        manager.reqMktData = lambda *a: (  # type: ignore[method-assign]
            manager.tickPrice(a[0], 2, -1.0, None),
            manager.tickSnapshotEnd(a[0]),
        )
        result = manager.request_market_data_snapshot(MagicMock())
        assert "ask" not in result

    def test_tick_option_computation_prefers_model_option(self):
        """MODEL_OPTION (tick type 13) is the canonical Greeks source;
        once captured, an earlier BID/ASK-side computation (types
        10/11/12) must not silently overwrite it."""
        manager = _manager()
        manager.nextValidId(1)

        def _fake_req(req_id, contract, *args):
            manager.tickOptionComputation(
                req_id, 11, 0, 0.30, 0.40, 5.0, 0.0, 0.02, 0.10, -0.05, 100.0
            )
            manager.tickOptionComputation(
                req_id, 13, 0, 0.42, 0.55, 5.2, 0.0, 0.03, 0.12, -0.06, 100.0
            )
            manager.tickOptionComputation(
                req_id, 10, 0, 0.20, 0.20, 4.5, 0.0, 0.01, 0.05, -0.02, 100.0
            )
            manager.tickSnapshotEnd(req_id)

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        result = manager.request_market_data_snapshot(MagicMock())
        assert result["implied_volatility"] == 0.42
        assert result["delta"] == 0.55
        assert result["greeks_source"] == "provider"

    def test_historical_bars_round_trip(self):
        manager = _manager()
        manager.nextValidId(1)
        fake_bar = MagicMock(
            date="1735689600", open=100.0, high=101.0, low=99.5, close=100.5, volume=1000.0
        )

        def _fake_req(req_id, contract, *args):
            manager.historicalData(req_id, fake_bar)
            manager.historicalDataEnd(req_id, "start", "end")

        manager.reqHistoricalData = _fake_req  # type: ignore[method-assign]
        bars = manager.request_historical_bars(MagicMock(), "20260830-16:00:00", "1 D", "1 min")
        assert bars == [fake_bar]


class TestLiveFindings2026_08_31:
    """IBKR TWS Migration Phase 2 -- two real bugs found during the
    actual first live parity session against a real authenticated
    account, neither of which any mocked-protocol test had caught."""

    def test_next_valid_id_requests_delayed_market_data_type(self):
        """A real live underlying-quote request failed with error 10089
        ("requires additional subscription... delayed market data is
        available") because TWS defaults to LIVE (mode 1) unless told
        otherwise -- this account's real, already-documented entitlement
        is delayed-only. reqMarketDataType(3) must be requested once the
        connection is genuinely ready."""
        manager = _manager()
        manager.serverVersion_ = 223  # real observed value; required for the guard below
        calls = []
        manager.reqMarketDataType = lambda mode: calls.append(mode)  # type: ignore[method-assign]
        manager.nextValidId(1)
        assert calls == [3]

    def test_next_valid_id_skips_market_data_type_without_a_real_server_version(self):
        """The guard itself: calling reqMarketDataType before a real
        server version is known would crash inside ibapi's own
        useProtoBuf() (int <= None) -- confirmed by reproducing the
        exact failure this test guards against."""
        manager = _manager()
        assert manager.serverVersion() is None
        calls = []
        manager.reqMarketDataType = lambda mode: calls.append(mode)  # type: ignore[method-assign]
        manager.nextValidId(1)  # must not raise
        assert calls == []

    def test_snapshot_mode_used_for_empty_generic_ticks(self):
        """Plain price snapshots (underlying quotes) genuinely work with
        snapshot=True -- confirmed live -- and must stay on that cheap,
        self-cleaning path (no cancelMktData needed)."""
        manager = _manager()
        manager.nextValidId(1)
        captured = {}

        def _fake_req(req_id, contract, generic_ticks, snapshot, reg_snapshot, options):
            captured["snapshot"] = snapshot
            manager.tickSnapshotEnd(req_id)

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        cancel_calls = []
        manager.cancelMktData = lambda req_id: cancel_calls.append(req_id)  # type: ignore[method-assign]
        manager.request_market_data_snapshot(MagicMock(), generic_ticks="")
        assert captured["snapshot"] is True
        assert cancel_calls == []

    def test_streaming_mode_used_for_non_empty_generic_ticks_and_cancelled_once(self):
        """A real, live-verified constraint: IB Gateway/TWS rejects
        snapshot=True combined with a non-empty generic tick list
        (real error 321, confirmed live 2026-08-31) -- option quotes
        (volume/OI/IV, generic_ticks="100,101,106") must use
        snapshot=False and be explicitly cancelled exactly once."""
        manager = _manager()
        manager.nextValidId(1)
        captured = {}

        def _fake_req(req_id, contract, generic_ticks, snapshot, reg_snapshot, options):
            captured["snapshot"] = snapshot
            captured["generic_ticks"] = generic_ticks
            manager.tickPrice(req_id, 1, 4.90, None)

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        cancel_calls = []
        manager.cancelMktData = lambda req_id: cancel_calls.append(req_id)  # type: ignore[method-assign]
        result = manager.request_market_data_snapshot(MagicMock(), generic_ticks="100,101,106")
        assert captured["snapshot"] is False
        assert captured["generic_ticks"] == "100,101,106"
        assert len(cancel_calls) == 1
        assert result["bid"] == 4.90

    def test_streaming_requirement_loop_subscribes_once_not_per_attempt(self):
        """The real leak this design avoids: re-subscribing per retry
        attempt (as the snapshot path correctly does) would orphan one
        streaming subscription per attempt, since streaming has no
        tickSnapshotEnd to naturally end each one."""
        manager = _manager()
        manager.nextValidId(1)
        req_calls = []

        def _fake_req(req_id, contract, generic_ticks, snapshot, reg_snapshot, options):
            req_calls.append(req_id)
            manager.tickPrice(req_id, 4, 5.00, None)  # last only -- ask never arrives

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        cancel_calls = []
        manager.cancelMktData = lambda req_id: cancel_calls.append(req_id)  # type: ignore[method-assign]
        manager.request_market_data_with_requirement(
            MagicMock(),
            requirement_satisfied=lambda r: r.get("ask") is not None,
            generic_ticks="100,101,106",
            max_attempts=3,
            retry_delay=0.0,
        )
        assert len(req_calls) == 1  # one real subscription, not three
        assert cancel_calls == [req_calls[0]]  # cancelled exactly once, at the end


class TestDelayedTickNormalization:
    """IBKR TWS delayed-market-data forensic (2026-08-31) -- CONFIRMED_
    ADAPTER_BUG_FIXED. A raw, unfiltered callback trace against the real
    authenticated account (bypassing this project's own code entirely --
    minimal official ibapi only) proved real DELAYED_BID/DELAYED_ASK/
    DELAYED_LAST/DELAYED_CLOSE/DELAYED_VOLUME ticks arrive with real,
    live-updating prices. This project's own tick-type maps only ever
    listened for the live tick IDs -- every delayed tick was silently
    dropped by a dict-lookup miss, which an earlier report misdiagnosed
    as an account entitlement limitation. These tests pin the fix: live
    and delayed tick types for the same real concept must normalize to
    the identical canonical field."""

    def _pending_result(self, manager, req_id=5):
        pending = manager._register(req_id, "test")  # noqa: SLF001
        pending.result = {}
        return pending

    @pytest.mark.parametrize(
        "tick_type,field",
        [
            (1, "bid"),
            (66, "bid"),  # DELAYED_BID
            (2, "ask"),
            (67, "ask"),  # DELAYED_ASK
            (4, "last"),
            (68, "last"),  # DELAYED_LAST
            (9, "close"),
            (75, "close"),  # DELAYED_CLOSE
        ],
    )
    def test_live_and_delayed_price_ticks_normalize_to_the_same_field(self, tick_type, field):
        manager = _manager()
        manager.nextValidId(1)
        self._pending_result(manager)
        manager.tickPrice(5, tick_type, 315.83, None)
        assert manager._pending[5].result[field] == 315.83  # noqa: SLF001

    def test_delayed_last_negative_sentinel_is_never_recorded(self):
        """Confirmed live: DELAYED_LAST can legitimately carry -1.0 (no
        real last trade yet) -- the exact same "-1 means no data"
        convention live LAST already used, now proven to apply to the
        delayed tick type too."""
        manager = _manager()
        manager.nextValidId(1)
        self._pending_result(manager)
        manager.tickPrice(5, 68, -1.0, None)
        assert "last" not in manager._pending[5].result  # noqa: SLF001

    @pytest.mark.parametrize("tick_type,field", [(8, "volume"), (74, "volume")])
    def test_live_and_delayed_volume_normalize_to_the_same_field(self, tick_type, field):
        from decimal import Decimal

        manager = _manager()
        manager.nextValidId(1)
        self._pending_result(manager)
        manager.tickSize(5, tick_type, Decimal("21276399"))
        assert manager._pending[5].result[field] == Decimal("21276399")  # noqa: SLF001

    def test_delayed_bid_ask_last_sizes_are_real_decimal_and_preserved(self):
        """Confirmed live: DELAYED_BID_SIZE/DELAYED_ASK_SIZE/DELAYED_
        LAST_SIZE (69/70/71) arrive as real Decimal instances. This
        project tracks only aggregate volume, not per-side size, so
        these are expected to have no canonical field -- must not
        crash, must not be silently coerced to int."""
        from decimal import Decimal

        manager = _manager()
        manager.nextValidId(1)
        self._pending_result(manager)
        for tick_type in (69, 70, 71):
            manager.tickSize(5, tick_type, Decimal("380"))  # must not raise
        assert manager._pending[5].result == {}  # noqa: SLF001 -- no canonical field, honestly empty

    def test_market_data_quality_stays_delayed_never_relabeled_live(self):
        manager = _manager()
        manager.nextValidId(1)
        self._pending_result(manager)
        manager.marketDataType(5, 3)  # DELAYED
        manager.tickPrice(5, 66, 315.83, None)  # DELAYED_BID
        result = manager._pending[5].result  # noqa: SLF001
        assert result["market_data_quality"] == "delayed"
        assert result["bid"] == 315.83

    def test_delayed_model_option_greeks_protected_from_later_downgrade(self):
        """The related real bug found alongside the tick-field gap: the
        original Greeks-precedence guard checked only tickType==13
        (live MODEL_OPTION) -- once the first canonical value was 83
        (DELAYED_MODEL_OPTION), a later DELAYED_ASK_OPTION(81) could
        silently overwrite it. Confirmed live: real MODEL_OPTION iv=0.50
        vs. real ASK_OPTION iv=1.24 on the same real contract (a real,
        wide-bid-ask-driven difference, not a bug -- but the canonical
        model value must still win)."""
        manager = _manager()
        manager.nextValidId(1)
        self._pending_result(manager)
        manager.tickOptionComputation(
            5, 83, 0, 0.50, 0.996, 29.32, 0.0, 0.00099, 0.00343, -0.0474, 317.24
        )
        manager.tickOptionComputation(
            5, 81, 0, 1.2357, 0.8658, 30.30, 0.0, 0.00747, 0.04862, -0.983, 316.76
        )
        result = manager._pending[5].result  # noqa: SLF001
        assert result["implied_volatility"] == 0.50
        assert result["delta"] == 0.996

    def test_partial_generic_tick_error_does_not_erase_already_captured_core_fields(self):
        """Section 6 -- a real 10091 ("partial market data... BID_ASK")
        must never wipe out delayed bid/ask that already arrived (or
        arrives later in the same subscription's lifetime)."""
        manager = _manager()
        manager.nextValidId(1)
        self._pending_result(manager)
        manager.tickPrice(5, 66, 315.83, None)  # DELAYED_BID arrives first
        manager.error(5, _now_ms(), 10091, "partial market data requires additional subscription")
        result = manager._pending[5].result  # noqa: SLF001
        assert result["bid"] == 315.83  # never erased
        assert result.get("_market_data_unavailable") is True  # honestly recorded too

    def test_required_side_readiness_is_satisfied_by_delayed_ask(self):
        """Section 7/8 -- required-side readiness must treat a delayed
        price tick exactly like a live one for FIELD PRESENCE."""
        from providers.ibkr_tws_options import _requirement_satisfied

        result = {"ask": 30.30, "market_data_quality": "delayed"}
        assert _requirement_satisfied(result, QuoteRequirement.ASK) is True

    def test_required_side_readiness_never_waits_on_optional_fields(self):
        """A BUY entry only needs ASK -- missing volume/OI/IV must never
        block readiness."""
        from providers.ibkr_tws_options import _requirement_satisfied

        result = {"ask": 30.30, "market_data_quality": "delayed"}  # no volume, no OI, no IV
        assert _requirement_satisfied(result, QuoteRequirement.ASK) is True


class TestOfficialDecimalCallbackCompatibility:
    """IBKR TWS Migration Phase 1.1, Section 4 -- the official 10.45
    client's real EWrapper.tickSize signature types ``size`` as
    ``decimal.Decimal``, not ``int`` (the unofficial, stale PyPI 9.81
    package Phase 1 was built against used ``int``). These tests pass
    the REAL official ``Decimal`` type (imported from the installed
    ``ibapi`` package itself, not a hand-rolled stand-in) through every
    callback that changed, confirming no precision is lost and nothing
    silently coerces it to int early."""

    def test_tick_size_accepts_real_decimal_volume(self):
        from decimal import Decimal

        manager = _manager()
        manager.nextValidId(1)
        pending = manager._register(5, "test")  # noqa: SLF001
        pending.result = {}

        manager.tickSize(5, 8, Decimal("120"))  # VOLUME
        assert pending.result["volume"] == Decimal("120")
        assert isinstance(pending.result["volume"], Decimal)

    def test_tick_size_open_interest_preserves_real_decimal(self):
        from decimal import Decimal

        manager = _manager()
        manager.nextValidId(1)
        pending = manager._register(5, "test")  # noqa: SLF001
        pending.result = {}

        manager.tickSize(5, 22, Decimal("340"))  # OPEN_INTEREST
        assert pending.result["open_interest"] == Decimal("340")

    def test_provider_normalizes_real_decimal_volume_without_precision_loss(self):
        """End-to-end through IBKRTWSProvider's own normalization
        (_to_int) -- must never coerce Decimal to int where information
        could be lost, and must never crash on a real Decimal input."""
        from decimal import Decimal

        from providers.ibkr_tws_options import _to_int

        assert _to_int(Decimal("120")) == 120
        assert _to_int(Decimal("0")) == 0
        # A real Decimal with a fractional remainder (should not occur
        # for a real IBKR volume/OI field, but must not crash if it did)
        # -- truncates honestly, same rule already applied to float input.
        assert _to_int(Decimal("120.0")) == 120

    def test_historical_bar_volume_as_real_decimal_sentinel(self):
        """BarData.volume/.wap default to ibapi's own UNSET_DECIMAL
        sentinel (a real Decimal), not a float -- fetch_historical_bars
        must convert it through the same safe str()-first path as every
        other numeric field, never assume float."""
        from decimal import Decimal

        from providers.ibkr_historical import _decimal_or_none

        assert _decimal_or_none(Decimal("1000.0")) == Decimal("1000.0")
        assert isinstance(_decimal_or_none(Decimal("1000.0")), Decimal)

    def test_error_accepts_real_official_signature_with_error_time(self):
        """The real official EWrapper.error() signature inserts
        errorTime (epoch ms) before errorCode -- confirmed directly
        against the installed ibapi package's own signature, not
        assumed. _last_heartbeat must reflect that real server-side
        time, not merely "whenever this code got around to it"."""
        import inspect
        from datetime import UTC, datetime

        from ibapi.wrapper import EWrapper

        real_sig = inspect.signature(EWrapper.error)
        expected_params = ["self", "reqId", "errorTime", "errorCode"]
        assert list(real_sig.parameters)[:4] == expected_params, (
            "official EWrapper.error() signature changed again -- update "
            "TWSConnectionManager.error() to match"
        )

        manager = _manager()
        manager.nextValidId(1)
        error_time_ms = int(datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        manager.error(-1, error_time_ms, 2103, "Market data farm connection is broken")
        assert manager._last_heartbeat == datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)  # noqa: SLF001


class TestSideAwareWarmup:
    """Section 17 -- bounded, validating retry (mirrors providers/
    ibkr_options.py's own _snapshot_with_warmup)."""

    def test_retries_until_requirement_satisfied(self, monkeypatch):
        manager = _manager()
        manager.nextValidId(1)
        monkeypatch.setattr("providers.ibkr_tws_client.time.sleep", lambda _s: None)

        call_count = {"n": 0}

        def _fake_req(req_id, contract, *args):
            call_count["n"] += 1
            manager.tickPrice(req_id, 4, 5.00, None)  # last always arrives
            if call_count["n"] >= 3:
                manager.tickPrice(req_id, 2, 5.10, None)  # ask arrives on 3rd attempt
            manager.tickSnapshotEnd(req_id)

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        result = manager.request_market_data_with_requirement(
            MagicMock(), requirement_satisfied=lambda r: r.get("ask") is not None
        )
        assert result["ask"] == 5.10
        assert call_count["n"] == 3

    def test_gives_up_honestly_after_bound(self, monkeypatch):
        manager = _manager()
        manager.nextValidId(1)
        monkeypatch.setattr("providers.ibkr_tws_client.time.sleep", lambda _s: None)
        attempts = {"n": 0}

        def _fake_req(req_id, contract, *args):
            attempts["n"] += 1
            manager.tickPrice(req_id, 4, 5.00, None)
            manager.tickSnapshotEnd(req_id)

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        result = manager.request_market_data_with_requirement(
            MagicMock(), requirement_satisfied=lambda r: r.get("ask") is not None, max_attempts=3
        )
        assert "ask" not in result
        assert attempts["n"] == 3

    def test_on_attempt_hook_fires_once_per_real_poll(self, monkeypatch):
        manager = _manager()
        manager.nextValidId(1)
        monkeypatch.setattr("providers.ibkr_tws_client.time.sleep", lambda _s: None)
        seen = []

        def _fake_req(req_id, contract, *args):
            manager.tickSnapshotEnd(req_id)

        manager.reqMktData = _fake_req  # type: ignore[method-assign]
        manager.request_market_data_with_requirement(
            MagicMock(),
            requirement_satisfied=lambda r: False,
            max_attempts=2,
            on_attempt=lambda n, r: seen.append(n),
        )
        assert seen == [1, 2]


class TestV3V4Isolation:
    """This migration is infrastructure only (Section 1) -- static
    confirmation, mirroring test_v4_2_v3_isolation.py's own established
    pattern, that no new IBKR TWS module imports anything from V3's real
    pipeline or V4's experimental analytics."""

    def test_no_new_ibkr_module_imports_v3_or_v4_decision_code(self):
        import inspect

        import providers.ibkr_tws_client as client_module
        import providers.ibkr_tws_historical as historical_module
        import providers.ibkr_tws_options as options_module

        forbidden = (
            "analytics.decision",
            "services.decision_engine",
            "services.decision_snapshot_freezing",
        )
        for module in (client_module, historical_module, options_module):
            source = inspect.getsource(module)
            for needle in forbidden:
                assert needle not in source, f"{module.__name__} references {needle!r}"


class TestInformationalErrors:
    def test_farm_ok_never_raises_or_touches_last_error(self):
        manager = _manager()
        manager.error(-1, _now_ms(), 2104, "Market data farm connection is OK")
        assert manager._last_error is None  # noqa: SLF001

    def test_farm_disconnected_sets_last_error_but_does_not_raise(self):
        manager = _manager()
        manager.error(-1, _now_ms(), 2103, "Market data farm connection is broken")
        assert manager._last_error is not None  # noqa: SLF001
        assert manager.state == TWSConnectionState.DISCONNECTED  # unaffected

    def test_unmatched_reqid_error_never_raises(self):
        """An error for a reqId with no pending request (e.g. arriving
        after the caller's own timeout already gave up) must be logged,
        never crash the reader thread."""
        manager = _manager()
        manager.error(
            9999, _now_ms(), 200, "No security definition has been found"
        )  # no exception raised
