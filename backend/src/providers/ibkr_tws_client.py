"""Connection manager for Interactive Brokers' TWS API (IB Gateway / TWS's
own stateful socket protocol) -- IBKR TWS Migration Phase 1.

READ ONLY, same guarantee as providers/ibkr_client.py (the existing Client
Portal Gateway integration this migration does NOT touch or remove): no
method anywhere in this module calls, or lets a caller reach, order
placement, order modification, order cancellation, order preview, or
option exercise. Defense in depth (see this migration's Phase 1 report,
Section 0): every dangerous method ``ibapi.client.EClient`` exposes
(``placeOrder``, ``cancelOrder``, ``reqOpenOrders``, ``reqGlobalCancel``,
``exerciseOptions``) is overridden below to raise immediately if ever
called, rather than merely relying on this module's own callers never
calling them. The higher-level provider adapter (providers/ibkr_tws_
options.py) exposes only read-only market-data/contract-discovery methods
-- there is no code path from that adapter's public surface to any of
these.

Library choice (documented per this migration's own Section 8 instruction
-- see the Phase 1.1 report, Section A, for the full correction writeup):
the OFFICIAL ``ibapi`` package -- version 10.45.1, fetched directly from
Interactive Brokers' own https://interactivebrokers.github.io download
(SHA256-verified by scripts/fetch_ibapi_official.py) and resolved as a
local path dependency (pyproject.toml's ``[tool.uv.sources]``), never
PyPI. Phase 1 originally depended on PyPI's ``ibapi==9.81.1.post1`` and
called it "official"; that was wrong, corrected in Phase 1.1 -- see
vendor/ibapi_official/PROVENANCE.md for the full record (PyPI
distribution is explicitly not hosted, endorsed, or supported by IBKR;
that package's own PyPI maintainer account is unrelated to Interactive
Brokers, despite its bundled metadata -- copied from IBKR's real source
-- claiming "Official"; it was also nine minor versions stale).

Chosen over an unofficial higher-level wrapper for the same underlying
reasons Phase 1 gave, which remain correct: (1) it is the only client
library IBKR itself publishes and documents error codes/callback
semantics against -- a wrapper would add a second, less-auditable
translation layer between this project's provider abstraction and
IBKR's real wire protocol; (2) this project needs only a small,
well-understood slice of the API (contract resolution, market-data
snapshots, historical bars) -- the smallest reliable dependency
surface, not a general-purpose trading framework built for order
management this project must never use; (3) it requires no additional
async runtime (no asyncio event loop competing with FastAPI's own) --
ibapi's reader thread is a plain blocking socket loop, run on one
dedicated background thread (see TWSConnectionManager.connect_and_start
below), which composes cleanly with this project's existing sync
SQLAlchemy/FastAPI stack.

Threading model (Section 40): ``ibapi.EClient.connect()`` opens the TCP
socket and spawns its own internal reader thread that only fills a queue;
nothing dispatches callbacks until something calls ``EClient.run()``,
which processes that queue and invokes the EWrapper callback methods below
-- so ``run()`` is started on exactly one dedicated daemon thread per
connection, never on the FastAPI event loop or the scheduler's own thread.
Every public method on this class that waits for a result (e.g.
``request_contract_details``) blocks the CALLING thread on a
``threading.Event`` with a bounded timeout, never the reader thread
itself -- an official scheduler job that calls one of these methods
blocks its own job execution (as it already does for the existing HTTP-
based Web provider), not the whole application.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ibapi.client import EClient
from ibapi.const import UNSET_DECIMAL
from ibapi.contract import Contract, ContractDetails
from ibapi.wrapper import EWrapper

from providers.ibkr_client import (
    IBKRClientIdInUseError,
    IBKRError,
    IBKRGatewayUnavailableError,
    IBKRMarketDataPermissionError,
    IBKRRateLimitedError,
)
from providers.ibkr_options import IBKRContractNotFoundError

logger = logging.getLogger(__name__)

# --- Well-known IBKR error codes (public API documentation, not guessed --
# see this migration's Phase 1 report, Section M, for the citation and the
# real taxonomy this maps into). Grouped by the Section 26 taxonomy
# category they classify into.
_CODES_AUTH_CONNECTION = {502, 504, 1100, 2110}
_CODES_SESSION_DISCONNECTED = {1100}
_CODES_FARM_DISCONNECTED = {2103, 2105, 2108}
_CODES_FARM_RECONNECTED = {1101, 1102, 2104, 2106, 2119, 2158, 2107}
_CODES_MARKET_DATA_PERMISSION = {354, 10168, 10197}
# 10089/10091 confirmed live (2026-08-31, real account): "requested
# [partial] market data requires additional subscription for API
# use... delayed market data is available" -- 10089 fires for the whole
# request, 10091 for a partial field group (e.g. BID_ASK specifically)
# -- the same real, honest "no live data" condition as 10167, just
# different real codes for it.
# 2188 confirmed live (2026-09-04, this account's real delayed-only
# entitlement): "Up-to-the-second historical data requires additional
# subscription for the API" is IBKR's own PRE-COMPLETION notice on a
# historical request -- it sits in IBKR's 2100-2200 warning band, and the
# real bars still arrive behind it. Classifying it as a SYSTEM_ERROR (the
# default for an unmapped code) raised on every historical request whose
# window reached near the present, which is exactly the request an
# end-of-day settlement close has to make.
_CODES_MARKET_DATA_UNAVAILABLE = {10089, 10091, 10167, 10182, 2119, 2188}
_CODES_RATE_LIMIT = {100, 509, 420}
_CODES_CONTRACT_NOT_FOUND = {200, 300}
_CODES_CLIENT_ID_IN_USE = {326}
_CODES_INFORMATIONAL = _CODES_FARM_RECONNECTED | {2100, 2101, 2102, 2137}

# Real, disclosed bound (never "sleep(2) then hope" -- Section 17). Mirrors
# providers/ibkr_options.py's own _SNAPSHOT_WARMUP_MAX_ATTEMPTS/_DELAY
# constants exactly, so both real IBKR adapters share one documented
# warm-up posture rather than two independently-tuned ones.
_OPTION_PROBE_GENERIC_TICKS = "100,101,106"
SNAPSHOT_WARMUP_MAX_ATTEMPTS = 5
SNAPSHOT_WARMUP_RETRY_DELAY_SECONDS = 1.5

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0

# Bounded reconnect backoff (Section 27) -- never immediate/unbounded
# retry against a live brokerage connection.
_RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
_RECONNECT_MAX_ATTEMPTS = len(_RECONNECT_BACKOFF_SECONDS)


class TWSConnectionState(enum.StrEnum):
    """Explicit connection lifecycle (Section 27) -- every state a caller
    (e.g. Operations health, Section 35) can observe and label honestly,
    never inferred."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"  # socket open, nextValidId not yet received
    READY = "ready"  # nextValidId received -- fully operational
    RECONNECTING = "reconnecting"
    FAILED = "failed"


def classify_error_code(code: int) -> str:
    """Maps one real TWS API error code into the Section 26 taxonomy.
    Never treats every IB error message as fatal -- several codes below
    (farm connection OK/reconnected, e.g. 2104/2106/2158) are IBKR's own
    informational connection-farm status messages, not failures."""
    if code in _CODES_CLIENT_ID_IN_USE:
        return "AUTH_CONNECTION"
    if code in _CODES_AUTH_CONNECTION:
        return "AUTH_CONNECTION"
    if code in _CODES_FARM_DISCONNECTED:
        return "FARM_DISCONNECTED"
    if code in _CODES_FARM_RECONNECTED:
        return "FARM_RECONNECTED"
    if code in _CODES_MARKET_DATA_PERMISSION:
        return "MARKET_DATA_PERMISSION"
    if code in _CODES_MARKET_DATA_UNAVAILABLE:
        return "MARKET_DATA_UNAVAILABLE"
    if code in _CODES_RATE_LIMIT:
        return "RATE_LIMIT"
    if code in _CODES_CONTRACT_NOT_FOUND:
        return "CONTRACT_NOT_FOUND"
    return "SYSTEM_ERROR"


def _exception_for_code(code: int, message: str) -> IBKRError | None:
    """Real exception to raise for a pending request's own error, or
    ``None`` for an informational/connection-level code that isn't a
    request failure at all (e.g. a farm-reconnected notice arriving with
    reqId=-1, never tied to a specific pending request)."""
    category = classify_error_code(code)
    if code in _CODES_CLIENT_ID_IN_USE:
        return IBKRClientIdInUseError(
            f"IB Gateway/TWS client id is already in use (error {code}): {message}"
        )
    if category == "AUTH_CONNECTION":
        return IBKRGatewayUnavailableError(
            f"could not reach / stay connected to IB Gateway/TWS (error {code}): {message}"
        )
    if category == "MARKET_DATA_PERMISSION":
        return IBKRMarketDataPermissionError(
            f"no market-data entitlement for this request (error {code}): {message}"
        )
    if category == "MARKET_DATA_UNAVAILABLE":
        return None  # handled by the caller as "no data", not an exception
    if category == "RATE_LIMIT":
        return IBKRRateLimitedError(
            f"IB Gateway/TWS rate-limited the request (error {code}): {message}"
        )
    if category == "CONTRACT_NOT_FOUND":
        return IBKRContractNotFoundError(f"no contract found (error {code}): {message}")
    if category in ("FARM_DISCONNECTED", "FARM_RECONNECTED"):
        return None  # informational connection-farm status, not a request failure
    return IBKRError(f"IB Gateway/TWS error {code}: {message}")


@dataclass
class _PendingRequest:
    """One in-flight, typed, bounded-lifecycle TWS request (Section 24) --
    never scattered callback state in an arbitrary dict. ``result`` is a
    mutable accumulator every relevant EWrapper callback appends/writes
    into for this ``request_id``; ``done`` is signalled exactly once, by
    whichever callback the request's kind treats as terminal (e.g.
    ``contractDetailsEnd``, ``tickSnapshotEnd``, ``historicalDataEnd``, or
    an ``error`` callback naming this reqId)."""

    request_id: int
    request_type: str
    started_at: float
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: IBKRError | None = None


@dataclass(frozen=True)
class TWSHealthSnapshot:
    """Section 35 -- what Operations/System Status needs to render TWS
    health consistently alongside the existing Web provider's IbkrStatus,
    without exposing account id, username, or session secrets (Section
    35's own explicit prohibition)."""

    provider: str  # always "tws"
    gateway_reachable: bool
    socket_connected: bool
    api_ready: bool
    market_data_quality_last_seen: str | None
    last_heartbeat: datetime | None
    last_error: str | None
    reconnect_state: str  # TWSConnectionState value


class TWSConnectionManager(EWrapper, EClient):
    """Owns exactly one long-lived TWS API connection (Section 9) -- never
    opens a new socket per request. ``host``/``port``/``client_id`` are
    fixed at construction (Section 11's deterministic-client-id rule);
    receiving ``nextValidId`` on connect does NOT authorize this project
    to place orders (Section 9) -- see the module docstring's defense-in-
    depth overrides below.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self._host = host
        self._port = port
        self._client_id = client_id
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout

        self._state_lock = threading.RLock()
        self._state = TWSConnectionState.DISCONNECTED
        self._reader_thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._startup_error: IBKRError | None = None

        self._req_id_lock = threading.Lock()
        self._next_req_id: int | None = None

        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}

        self._last_heartbeat: datetime | None = None
        self._last_error: str | None = None
        self._last_market_data_quality: str | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @property
    def state(self) -> TWSConnectionState:
        with self._state_lock:
            return self._state

    def _set_state(self, new_state: TWSConnectionState) -> None:
        with self._state_lock:
            self._state = new_state

    def connect_and_start(self) -> None:
        """Idempotent: a no-op if already CONNECTED/READY. Blocks the
        calling thread only until ``nextValidId`` arrives (real API
        readiness -- Section 9), up to ``connect_timeout``, then returns.
        Raises a real, typed IBKRError on genuine failure (gateway
        unreachable, client id already in use) -- never silently leaves
        the manager in a half-connected state."""
        if self.state in (TWSConnectionState.CONNECTED, TWSConnectionState.READY):
            return
        self._set_state(TWSConnectionState.CONNECTING)
        self._ready_event.clear()
        self._startup_error = None
        try:
            self.connect(self._host, self._port, self._client_id)
        except OSError as exc:
            self._set_state(TWSConnectionState.FAILED)
            self._last_error = str(exc)
            raise IBKRGatewayUnavailableError(
                f"could not reach IB Gateway/TWS at {self._host}:{self._port} -- "
                "is it running and logged in? See this project's IBKR TWS "
                "Migration Phase 1 report, Section O."
            ) from exc

        self._reader_thread = threading.Thread(target=self.run, name="ibkr-tws-reader", daemon=True)
        self._reader_thread.start()

        ready = self._ready_event.wait(timeout=self._connect_timeout)
        if self._startup_error is not None:
            self._set_state(TWSConnectionState.FAILED)
            raise self._startup_error
        if not ready:
            self._set_state(TWSConnectionState.FAILED)
            self._last_error = "timed out waiting for nextValidId"
            raise IBKRGatewayUnavailableError(
                f"connected to IB Gateway/TWS at {self._host}:{self._port} but no "
                f"nextValidId arrived within {self._connect_timeout}s -- Gateway may "
                "not be logged in yet (manual authentication required)"
            )

    def ensure_connected(self) -> None:
        """Bounded-backoff reconnection (Section 27) -- never immediate,
        never unbounded. A no-op when already READY. Raises the real,
        final connection error if every bounded attempt fails."""
        if self.state == TWSConnectionState.READY:
            return
        self._set_state(TWSConnectionState.RECONNECTING)
        last_exc: IBKRError | None = None
        for attempt, delay in enumerate(_RECONNECT_BACKOFF_SECONDS, start=1):
            try:
                self.connect_and_start()
                return
            except IBKRError as exc:
                last_exc = exc
                logger.warning(
                    "IBKR TWS reconnect attempt %d/%d failed: %s",
                    attempt,
                    _RECONNECT_MAX_ATTEMPTS,
                    exc,
                )
                if attempt < _RECONNECT_MAX_ATTEMPTS:
                    time.sleep(delay)
        self._set_state(TWSConnectionState.FAILED)
        assert last_exc is not None
        raise last_exc

    def shutdown(self) -> None:
        """Clean disconnect -- never called automatically mid-session;
        an explicit lifecycle action (FastAPI shutdown, Section 41)."""
        try:
            self.disconnect()
        finally:
            self._set_state(TWSConnectionState.DISCONNECTED)

    def health_snapshot(self) -> TWSHealthSnapshot:
        state = self.state
        # IBKR TWS Migration Phase 2, Section 11 -- found while wiring up
        # the persistent health probe: ``state != DISCONNECTED`` wrongly
        # reported FAILED (e.g. a client-id collision, or a real socket
        # error) as "reachable" too, since FAILED is also not DISCONNECTED.
        # ``isConnected()`` is ibapi's own real, authoritative signal for
        # whether a TCP connection actually exists right now -- true for
        # CONNECTED/READY (and for a client-id collision, since that
        # rejection happens only after the socket itself connected fine),
        # false for a genuine socket-level failure (e.g. connection
        # refused) or a plain unauthenticated timeout with no real
        # connection at all.
        socket_connected = self.isConnected() if hasattr(self, "isConnected") else False
        return TWSHealthSnapshot(
            provider="tws",
            gateway_reachable=socket_connected,
            socket_connected=socket_connected,
            api_ready=state == TWSConnectionState.READY,
            market_data_quality_last_seen=self._last_market_data_quality,
            last_heartbeat=self._last_heartbeat,
            last_error=self._last_error,
            reconnect_state=state.value,
        )

    # ------------------------------------------------------------------
    # Request-ID allocation (Section 23) and pending-request registry
    # (Section 24)
    # ------------------------------------------------------------------

    def next_request_id(self) -> int:
        with self._req_id_lock:
            if self._next_req_id is None:
                raise IBKRGatewayUnavailableError(
                    "no request id available yet -- connect_and_start() must "
                    "succeed (and receive nextValidId) before making requests"
                )
            req_id = self._next_req_id
            self._next_req_id += 1
            return req_id

    def _register(self, request_id: int, request_type: str) -> _PendingRequest:
        pending = _PendingRequest(
            request_id=request_id, request_type=request_type, started_at=time.monotonic()
        )
        with self._pending_lock:
            self._pending[request_id] = pending
        return pending

    def _pop(self, request_id: int) -> _PendingRequest | None:
        with self._pending_lock:
            return self._pending.pop(request_id, None)

    def _peek(self, request_id: int) -> _PendingRequest | None:
        with self._pending_lock:
            return self._pending.get(request_id)

    def _wait(self, pending: _PendingRequest, timeout: float | None) -> None:
        bound = timeout if timeout is not None else self._request_timeout
        finished = pending.done.wait(timeout=bound)
        self._pop(pending.request_id)
        if not finished:
            raise IBKRGatewayUnavailableError(
                f"IB Gateway/TWS request timed out after {bound}s "
                f"(request_type={pending.request_type!r}, reqId={pending.request_id})"
            )
        if pending.error is not None:
            raise pending.error

    # ------------------------------------------------------------------
    # EWrapper callbacks
    # ------------------------------------------------------------------

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 (ibapi's own casing)
        with self._req_id_lock:
            self._next_req_id = orderId
        self._set_state(TWSConnectionState.READY)
        self._last_heartbeat = datetime.now(UTC)
        # IBKR TWS Migration Phase 2 -- a real bug found in first live
        # testing (2026-08-31): Phase 1's own PROVENANCE notes documented
        # "defaults to requesting DELAYED (3)" as the intended startup
        # behavior, matching this account's own real, already-documented
        # delayed-only options entitlement (see docs/ibkr_integration.md
        # and MarketDataQualityPolicy's own docstring) -- but the actual
        # call was never made. Without it, TWS defaults to LIVE (1),
        # which a real live request against this account rejected with
        # error 10089 ("requested market data requires additional
        # subscription... delayed market data is available") on every
        # single underlying quote. The real marketDataType() callback
        # value is still what gets reported per request (never a
        # hardcoded label) -- a genuinely live-entitled symbol still
        # reports live even under this default request.
        #
        # Guarded on a real serverVersion() being available: ibapi's own
        # real connection handshake always sets it (inside EClient.
        # connect(), before the reader thread can ever dispatch
        # nextValidId) -- so in real usage this guard is always true and
        # changes nothing. It matters for a caller (this project's own
        # tests included) that invokes nextValidId() directly without a
        # real handshake -- calling reqMarketDataType before a real
        # server version is known would otherwise crash inside ibapi's
        # own useProtoBuf() (int <= None).
        if self.serverVersion() is not None:
            self.reqMarketDataType(3)
        self._ready_event.set()

    def connectAck(self) -> None:  # noqa: N802
        self._set_state(TWSConnectionState.CONNECTED)

    def connectionClosed(self) -> None:  # noqa: N802
        self._set_state(TWSConnectionState.DISCONNECTED)
        self._last_error = "connection closed"
        # Unblock anything still waiting -- a closed socket will never
        # deliver the callback a pending request is waiting on.
        with self._pending_lock:
            pending_requests = list(self._pending.values())
        for pending in pending_requests:
            pending.error = IBKRGatewayUnavailableError(
                "IB Gateway/TWS connection closed while this request was in flight"
            )
            pending.done.set()

    def error(  # noqa: N802
        self,
        reqId: int,
        errorTime: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        # IBKR TWS Migration Phase 1.1 -- the official 10.45 client's
        # real EWrapper.error() signature inserts errorTime (epoch ms)
        # before errorCode; Phase 1 was built and tested against the
        # unofficial, stale PyPI 9.81 package, whose error() had no such
        # parameter -- every real error dispatch would have silently
        # misread errorCode as errorTime and errorString as errorCode
        # under the real official client. Caught only by this
        # correction's own dependency-source audit, never by a test,
        # since every prior test called this method directly with the
        # old (wrong) argument order rather than exercising it through a
        # real ibapi dispatch. errorTime is a real, authoritative
        # server-side timestamp -- used here in preference to "whenever
        # this project's own code happened to process the callback."
        self._last_heartbeat = datetime.fromtimestamp(errorTime / 1000, tz=UTC)
        category = classify_error_code(errorCode)
        if errorCode in _CODES_INFORMATIONAL:
            logger.info("IBKR TWS informational (code %d): %s", errorCode, errorString)
            if category == "FARM_DISCONNECTED":
                self._last_error = f"[{errorCode}] {errorString}"
            return

        self._last_error = f"[{errorCode}] {errorString}"
        exc = _exception_for_code(errorCode, errorString)

        if errorCode in _CODES_CLIENT_ID_IN_USE and reqId in (-1, 0):
            # A startup-time failure (before nextValidId), not tied to a
            # specific request -- surfaced through connect_and_start()
            # instead of a pending request.
            self._startup_error = exc
            self._ready_event.set()
            return

        pending = self._peek(reqId)
        if pending is not None:
            if exc is not None:
                pending.error = exc
                pending.done.set()
            else:
                # e.g. MARKET_DATA_UNAVAILABLE -- a real, honest,
                # PRE-COMPLETION notice, not an exception and NOT the
                # request's own terminal signal. A real, live-verified
                # bug found here (delayed-market-data forensic,
                # 2026-08-31): this branch used to call
                # ``pending.done.set()`` immediately, which for a
                # snapshot-mode request (empty generic_ticks, e.g.
                # underlying quotes) completed the wait the moment this
                # informational-ish notice arrived -- BEFORE the real
                # delayed ticks that follow it. Confirmed with a raw,
                # unfiltered ibapi trace: code 10167 arrived at t+0.248s,
                # the real DELAYED_BID/ASK/LAST ticks at t+0.584s, and
                # the real terminal ``tickSnapshotEnd`` only at t+0.587s
                # -- IBKR's own snapshot protocol keeps a request open
                # well past this notice and reliably still delivers a
                # real tickSnapshotEnd (well inside this manager's own
                # request-timeout bound) whether or not any further data
                # ever arrives. Recording the honest flag here and
                # otherwise doing nothing lets the REAL terminal signal
                # (tickSnapshotEnd for snapshot mode; the bounded retry
                # loop's own timer for streaming mode, which never
                # waited on ``done`` in the first place) decide when the
                # request is actually finished.
                # Only a market-data request accumulates into a dict. A
                # historical request accumulates into a LIST of bars, and
                # coercing that to {} here would make the very next
                # historicalData callback raise on .append -- so the
                # accumulator's own shape is left strictly alone.
                if pending.result is None and pending.request_type.startswith("market_data"):
                    pending.result = {}
                if isinstance(pending.result, dict):
                    pending.result["_market_data_unavailable"] = True
        elif reqId in (-1, 0) and exc is not None:
            logger.warning("IBKR TWS connection-level error (code %d): %s", errorCode, errorString)

    # ------------------------------------------------------------------
    # Defense in depth (Section 0 / Section 10) -- these ARE real methods
    # on ibapi.client.EClient (the base class this manager must inherit
    # from to receive any callback at all); overriding them here is what
    # makes "no order operation is possible through the new provider
    # interface" a real, enforced guarantee rather than a convention this
    # module's own callers merely promise to follow.
    # ------------------------------------------------------------------

    def placeOrder(self, *args, **kwargs) -> None:  # noqa: N802
        raise RuntimeError("read-only IBKR TWS provider: order placement is permanently disabled")

    def cancelOrder(self, *args, **kwargs) -> None:  # noqa: N802
        raise RuntimeError(
            "read-only IBKR TWS provider: order cancellation is permanently disabled"
        )

    def reqOpenOrders(self, *args, **kwargs) -> None:  # noqa: N802
        raise RuntimeError("read-only IBKR TWS provider: order queries are permanently disabled")

    def reqGlobalCancel(self, *args, **kwargs) -> None:  # noqa: N802
        raise RuntimeError(
            "read-only IBKR TWS provider: order cancellation is permanently disabled"
        )

    def exerciseOptions(self, *args, **kwargs) -> None:  # noqa: N802
        raise RuntimeError("read-only IBKR TWS provider: option exercise is permanently disabled")

    # ------------------------------------------------------------------
    # Contract details (Section 15 -- exact contract resolution)
    # ------------------------------------------------------------------

    def contractDetails(self, reqId: int, contractDetails: ContractDetails) -> None:  # noqa: N802
        pending = self._peek(reqId)
        if pending is None:
            return
        if pending.result is None:
            pending.result = []
        pending.result.append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        pending = self._peek(reqId)
        if pending is None:
            return
        if pending.result is None:
            pending.result = []
        pending.done.set()

    def request_contract_details(
        self, contract: Contract, timeout: float | None = None
    ) -> list[ContractDetails]:
        req_id = self.next_request_id()
        pending = self._register(req_id, "contract_details")
        self.reqContractDetails(req_id, contract)
        self._wait(pending, timeout)
        return pending.result or []

    # ------------------------------------------------------------------
    # Security-definition option parameters (Section 13/14 -- broad
    # strike/expiration METADATA discovery, in one real call, that
    # already returns the COMPLETE listed set -- see this migration's
    # Phase 1 report, Section G, for why this is a genuine improvement
    # over the Web adapter's own per-month-window strikes call).
    # ------------------------------------------------------------------

    def securityDefinitionOptionParameter(  # noqa: N802
        self,
        reqId: int,
        exchange: str,
        underlyingConId: int,
        tradingClass: str,
        multiplier: str,
        expirations: set,
        strikes: set,
    ) -> None:
        pending = self._peek(reqId)
        if pending is None:
            return
        if pending.result is None:
            pending.result = []
        pending.result.append(
            {
                "exchange": exchange,
                "underlying_conid": underlyingConId,
                "trading_class": tradingClass,
                "multiplier": multiplier,
                "expirations": set(expirations),
                "strikes": set(strikes),
            }
        )

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:  # noqa: N802
        pending = self._peek(reqId)
        if pending is None:
            return
        if pending.result is None:
            pending.result = []
        pending.done.set()

    def request_sec_def_opt_params(
        self,
        underlying_symbol: str,
        underlying_sec_type: str,
        underlying_conid: int,
        futures_fop_exchange: str = "",
        timeout: float | None = None,
    ) -> list[dict]:
        req_id = self.next_request_id()
        pending = self._register(req_id, "sec_def_opt_params")
        self.reqSecDefOptParams(
            req_id, underlying_symbol, futures_fop_exchange, underlying_sec_type, underlying_conid
        )
        self._wait(pending, timeout)
        return pending.result or []

    # ------------------------------------------------------------------
    # Market-data snapshot (Section 12/17 -- bounded, side-aware
    # readiness; Section 18 -- market-data-quality mode)
    # ------------------------------------------------------------------

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        pending = self._peek(reqId)
        quality = _MARKET_DATA_TYPE_TO_QUALITY.get(marketDataType, "unknown")
        self._last_market_data_quality = quality
        if pending is not None and isinstance(pending.result, dict):
            pending.result["market_data_quality"] = quality

    def _trace_raw_tick(self, req_id: int, kind: str, tick_type: int, value: object) -> None:
        """Opt-in forensic capture of the RAW wire tick, before any
        normalization drops or rewrites it (V4 required-side incident,
        2026-09-04). Inert unless a caller pre-seeded ``_raw_ticks`` on
        the pending result -- the production quote path never does, so
        this adds one list lookup per tick and nothing else."""
        pending = self._peek(req_id)
        if pending is None or not isinstance(pending.result, dict):
            return
        trace = pending.result.get("_raw_ticks")
        if isinstance(trace, list):
            trace.append(
                {
                    "kind": kind,
                    "tick_type": int(tick_type),
                    "value": value,
                    "at": datetime.now(UTC).isoformat(),
                }
            )

    def tickPrice(  # noqa: N802
        self, reqId: int, tickType: int, price: float, attrib
    ) -> None:
        self._trace_raw_tick(reqId, "price", tickType, price)
        self._write_tick(reqId, _PRICE_TICK_FIELDS.get(tickType), price)

    def tickSize(self, reqId: int, tickType: int, size: int) -> None:  # noqa: N802
        self._trace_raw_tick(reqId, "size", tickType, size)
        self._write_tick(reqId, _SIZE_TICK_FIELDS.get(tickType), size)

    def tickGeneric(self, reqId: int, tickType: int, value: float) -> None:  # noqa: N802
        self._trace_raw_tick(reqId, "generic", tickType, value)
        self._write_tick(reqId, _GENERIC_TICK_FIELDS.get(tickType), value)

    def tickOptionComputation(  # noqa: N802
        self,
        reqId: int,
        tickType: int,
        tickAttrib: int,
        impliedVol: float,
        delta: float,
        optPrice: float,
        pvDividend: float,
        gamma: float,
        vega: float,
        theta: float,
        undPrice: float,
    ) -> None:
        pending = self._peek(reqId)
        if pending is None or not isinstance(pending.result, dict):
            return
        # Prefer MODEL_OPTION (13, live) / DELAYED_MODEL_OPTION (83,
        # delayed -- confirmed live 2026-08-31: a real DELAYED_MODEL_
        # OPTION tick arrives with a genuine, complete Greeks set);
        # accept whichever computation arrives first if neither ever
        # does (a real, honest "best available" -- never a fabricated
        # Greek). All eight *_OPTION_COMPUTATION tick types (10/11/12/13
        # live, 80/81/82/83 delayed) carry the identical field set per
        # the TWS API. A real, live-verified bug found here: the
        # original guard checked only ``== 13``, so once the FIRST
        # canonical value happened to be 83 (delayed) rather than 13
        # (live), a later BID/ASK/LAST-side computation (e.g. real
        # DELAYED_ASK_OPTION, confirmed live to report a materially
        # different IV -- 1.24 vs. MODEL_OPTION's 0.50 on the same real
        # contract, since ask-side IV reflects the wide real bid-ask
        # spread, not a bug -- just a different real number) could
        # silently overwrite it. Both canonical tick types are now
        # protected identically.
        if pending.result.get("_greeks_tick_type") in (13, 83) and tickType not in (13, 83):
            return
        if impliedVol not in (None, -1) and impliedVol == impliedVol:  # NaN-safe
            pending.result["implied_volatility"] = impliedVol
        if delta not in (None, -2) and delta == delta:
            pending.result["delta"] = delta
        if gamma not in (None, -2) and gamma == gamma:
            pending.result["gamma"] = gamma
        if vega not in (None, -2) and vega == vega:
            pending.result["vega"] = vega
        if theta not in (None, -2) and theta == theta:
            pending.result["theta"] = theta
        pending.result["_greeks_tick_type"] = tickType
        pending.result["greeks_source"] = "provider"

    def tickString(self, reqId: int, tickType: int, value: str) -> None:  # noqa: N802
        pending = self._peek(reqId)
        if pending is None or not isinstance(pending.result, dict):
            return
        if tickType == 45:  # LAST_TIMESTAMP
            pending.result["last_timestamp_epoch"] = value

    def tickSnapshotEnd(self, reqId: int) -> None:  # noqa: N802
        pending = self._peek(reqId)
        if pending is None:
            return
        if pending.result is None:
            pending.result = {}
        pending.done.set()

    def _write_tick(self, req_id: int, field_name: str | None, value: float) -> None:
        if field_name is None:
            return
        pending = self._peek(req_id)
        if pending is None:
            return
        if pending.result is None:
            pending.result = {}
        if isinstance(pending.result, dict) and value is not None and value == value:  # NaN-safe
            if value < 0 and field_name in ("bid", "ask", "last", "close"):
                # IB sends -1 for "no data" on price ticks -- never a real
                # price, so it is still never written as one. But -1 is NOT
                # silence: on the BID/ASK sides it is IBKR's own explicit
                # statement that the book has no order on that side at all,
                # and it arrives paired with a size tick of 0. Proven live
                # on 2026-09-04 (V4 required-side settlement incident):
                # five deep-OTM legs each delivered tickPrice(66)=-1 with
                # tickSize(69)=0 while the ask side quoted normally, and
                # control contracts on the same delayed feed delivered real
                # bids of 0.65 and 0.01 with real sizes. Collapsing that
                # explicit "no bid exists" into "no tick arrived" made an
                # answered request look unanswered, so the bounded warm-up
                # burned all five attempts and the failure was mislabelled
                # as a missing quote. Record the sentinel as the real
                # observation it is; the price itself stays unwritten.
                if field_name in ("bid", "ask"):
                    pending.result[f"{field_name}_no_data_sentinel"] = True
                return
            if value == UNSET_DECIMAL:
                # IBKR TWS Migration Phase 1.1, Section 4 -- the official
                # client's real Decimal-typed size fields (volume, open
                # interest) use this real sentinel (2**127-1) for "no
                # data" instead of -1 -- confirmed directly from the
                # installed ibapi package's own decoder.py. Never record
                # it as a real quantity.
                return
            pending.result[field_name] = value

    def request_market_data_snapshot(
        self, contract: Contract, generic_ticks: str = "", timeout: float | None = None
    ) -> dict:
        """A real, live-verified constraint (2026-08-31, this migration's
        Phase 2): IB Gateway/TWS rejects ``reqMktData(snapshot=True)``
        combined with a non-empty generic tick list with a real error
        321, "snapshot market data subscription is not applicable to
        generic ticks" -- confirmed live against a real authenticated
        account, contradicting Phase 1's own (untested, docs-derived)
        assumption that "most generic ticks work with snapshot requests
        since API 973+". Empty ``generic_ticks`` (a plain price
        snapshot) genuinely does work in snapshot mode -- confirmed live
        too -- and needs no ``cancelMktData`` (IB Gateway/TWS auto-
        terminates it after ``tickSnapshotEnd``). A non-empty
        ``generic_ticks`` list therefore uses a genuine STREAMING
        subscription instead (``snapshot=False``), which has no terminal
        callback of its own -- bounded here to one ``request_timeout``-
        wide wait, then explicitly cancelled via ``cancelMktData`` so no
        subscription is ever left open. Volume/OI/IV(106) all require
        this path; Greeks arrive via ``tickOptionComputation`` either way.
        """
        use_streaming = bool(generic_ticks)
        req_id = self.next_request_id()
        pending = self._register(
            req_id, "market_data_streaming" if use_streaming else "market_data_snapshot"
        )
        pending.result = {}
        self.reqMktData(req_id, contract, generic_ticks, not use_streaming, False, [])
        try:
            if use_streaming:
                bound = timeout if timeout is not None else self._request_timeout
                pending.done.wait(timeout=min(bound, 1.0))
                if pending.error is not None:
                    raise pending.error
            else:
                self._wait(pending, timeout)
        finally:
            if use_streaming:
                self.cancelMktData(req_id)
                self._pop(req_id)
        return pending.result or {}

    def probe_market_data_ticks(self, contract: Contract, seconds: float) -> dict:
        """READ-ONLY forensic probe (V4 required-side incident): one real
        streaming subscription on the shared production connection, held
        for ``seconds``, returning every RAW tick seen alongside the
        normalized accumulator. Writes nothing, places nothing, and is
        cancelled exactly once in ``finally`` like every other streaming
        request here."""
        self.ensure_connected()
        req_id = self.next_request_id()
        pending = self._register(req_id, "market_data_streaming")
        pending.result = {"_raw_ticks": []}
        started = time.monotonic()
        self.reqMktData(req_id, contract, _OPTION_PROBE_GENERIC_TICKS, False, False, [])
        try:
            deadline = started + max(0.5, min(seconds, 45.0))
            while time.monotonic() < deadline:
                time.sleep(0.25)
                if pending.error is not None:
                    break
            result = dict(pending.result) if isinstance(pending.result, dict) else {}
            result["_probe_error"] = None if pending.error is None else str(pending.error)
            result["_probe_seconds"] = round(time.monotonic() - started, 3)
            return result
        finally:
            self.cancelMktData(req_id)
            self._pop(req_id)

    def request_market_data_with_requirement(
        self,
        contract: Contract,
        requirement_satisfied: Callable[[dict], bool],
        generic_ticks: str = "",
        max_attempts: int = SNAPSHOT_WARMUP_MAX_ATTEMPTS,
        retry_delay: float = SNAPSHOT_WARMUP_RETRY_DELAY_SECONDS,
        on_attempt: Callable[[int, dict], None] | None = None,
        timeout: float | None = None,
        requirement_terminal: Callable[[dict], bool] | None = None,
    ) -> dict:
        """Bounded, validating retry (Section 17) -- mirrors providers/
        ibkr_options.py's own ``_snapshot_with_warmup`` exactly: never
        ``sleep(2)`` then hope. For a plain price snapshot (empty
        ``generic_ticks``), each attempt is a fresh, self-cleaning
        ``request_market_data_snapshot`` call -- unchanged real Phase 1
        behavior. For a real option quote (non-empty ``generic_ticks``,
        e.g. volume/OI/IV), streaming market data requires subscribing
        ONCE and polling the SAME accumulating result across attempts --
        re-subscribing per attempt (as the snapshot path does) would
        leak one orphaned streaming subscription per attempt, since a
        streaming request has no self-terminating counterpart to
        snapshot's own ``tickSnapshotEnd``. Cancelled exactly once, at
        the end, regardless of which attempt satisfied the requirement.
        """
        if not generic_ticks:
            result: dict = {}
            for attempt in range(1, max_attempts + 1):
                result = self.request_market_data_snapshot(contract, generic_ticks, timeout)
                if on_attempt is not None:
                    on_attempt(attempt, result)
                if requirement_satisfied(result):
                    break
                if requirement_terminal is not None and requirement_terminal(result):
                    break  # IBKR has answered definitively -- retrying cannot change it
                if attempt < max_attempts:
                    time.sleep(retry_delay)
            return result

        req_id = self.next_request_id()
        pending = self._register(req_id, "market_data_streaming")
        pending.result = {}
        self.reqMktData(req_id, contract, generic_ticks, False, False, [])
        try:
            result = {}
            for attempt in range(1, max_attempts + 1):
                time.sleep(retry_delay)
                if pending.error is not None:
                    raise pending.error
                result = dict(pending.result) if isinstance(pending.result, dict) else {}
                if on_attempt is not None:
                    on_attempt(attempt, result)
                if requirement_satisfied(result):
                    break
                if requirement_terminal is not None and requirement_terminal(result):
                    break  # IBKR has answered definitively -- retrying cannot change it
            return result
        finally:
            self.cancelMktData(req_id)
            self._pop(req_id)

    # ------------------------------------------------------------------
    # Historical bars (Section 38)
    # ------------------------------------------------------------------

    def historicalData(self, reqId: int, bar) -> None:  # noqa: N802
        pending = self._peek(reqId)
        if pending is None:
            return
        if pending.result is None:
            pending.result = []
        pending.result.append(bar)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802
        pending = self._peek(reqId)
        if pending is None:
            return
        if pending.result is None:
            pending.result = []
        pending.done.set()

    def request_historical_bars(
        self,
        contract: Contract,
        end_datetime: str,
        duration: str,
        bar_size: str,
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        timeout: float | None = None,
    ) -> list:
        req_id = self.next_request_id()
        pending = self._register(req_id, "historical_data")
        # formatDate=2 -- bar.date arrives as a Unix epoch-seconds string,
        # parsed unambiguously below; formatDate=1's locale-formatted
        # string would need timezone-aware parsing this project has no
        # reason to duplicate (Section 39 -- no naive datetimes).
        self.reqHistoricalData(
            req_id,
            contract,
            end_datetime,
            duration,
            bar_size,
            what_to_show,
            1 if use_rth else 0,
            2,
            False,
            [],
        )
        self._wait(pending, timeout)
        return pending.result or []


# Tick-type -> normalized field name maps (Section 20/21) -- the
# TickTypeEnum positions confirmed directly from ibapi's own
# ibapi/ticktype.py (see this migration's Phase 1 report, Section D).
#
# A real, live-verified bug (found 2026-08-31, forensic task): this map
# originally carried ONLY the live tick types (1/2/4/9). A real,
# unfiltered callback trace against a real authenticated Gateway (this
# account's own delayed-only entitlement) proved that IBKR correctly
# sends DELAYED_BID(66)/DELAYED_ASK(67)/DELAYED_LAST(68)/DELAYED_
# CLOSE(75) with real, live-updating prices -- this project's own
# adapter was silently dropping every one of them, since a dict lookup
# miss short-circuits _write_tick before it ever writes a field. This
# was misdiagnosed as an account entitlement limitation in an earlier
# report; it was a normalization bug the whole time. Both the live and
# delayed tick type for the same real concept map to the identical
# canonical field -- callers never see which one arrived, only
# ``market_data_quality`` (set separately, from the real
# ``marketDataType`` callback) discloses that.
_PRICE_TICK_FIELDS: dict[int, str] = {
    1: "bid",
    66: "bid",  # DELAYED_BID
    2: "ask",
    67: "ask",  # DELAYED_ASK
    4: "last",
    68: "last",  # DELAYED_LAST
    9: "close",
    75: "close",  # DELAYED_CLOSE
}
_SIZE_TICK_FIELDS: dict[int, str] = {
    # Bid/ask depth-of-one sizes: the corroborating half of the empty-book
    # signal above (a -1 price with a 0 size is IBKR saying "nothing is
    # bid", not "nothing arrived"). Both the live and delayed tick type for
    # the same real concept map to the identical canonical field, exactly
    # as _PRICE_TICK_FIELDS already does.
    0: "bid_size",
    69: "bid_size",  # DELAYED_BID_SIZE
    3: "ask_size",
    70: "ask_size",  # DELAYED_ASK_SIZE
    8: "volume",
    74: "volume",  # DELAYED_VOLUME -- confirmed live: a real Decimal (e.g. 21,276,399 shares)
    22: "open_interest",  # no distinct delayed variant exists for open interest
}
_GENERIC_TICK_FIELDS: dict[int, str] = {
    24: "implied_volatility_generic",  # OPTION_IMPLIED_VOL (generic tick 106);
    # tickOptionComputation (real per-leg Greeks) is preferred when it
    # arrives -- see tickOptionComputation's own comment above -- this is
    # kept only as an honest fallback label, never silently substituted
    # for a provider Greek.
}

# TWS reqMarketDataType() modes -> this project's canonical
# MarketDataQuality vocabulary (Section 18/19) -- mirrors providers/
# ibkr_client.py::decode_market_data_quality's own precedent of
# collapsing multiple real IBKR states into "frozen" (that function
# already does this for Z/Y); DELAYED_FROZEN(4) here follows the exact
# same precedent for the identical reason (a stale delayed value).
_MARKET_DATA_TYPE_TO_QUALITY: dict[int, str] = {
    1: "live",
    2: "frozen",
    3: "delayed",
    4: "frozen",
}
