"""One long-lived TWS connection, reused across every health check --
IBKR TWS Migration Phase 2, Section 11.

Phase 1's own ``services.system_status.get_tws_status`` did a real
connect -> probe -> disconnect on every single call, with no memory of
the last attempt. Audited directly against this project's real frontend
code (not assumed): ``GET /system-status`` (where Phase 1 put the new
``tws`` field) is fetched once per page mount via ``useAsync(..., [])``
on three real pages (``DataStatus.tsx``, ``Settings/Ibkr.tsx``,
``EarningsAnalystDashboard.tsx``) -- today, in what's actually deployed,
that field creates no real polling churn. ``pages/Operations.tsx``,
however, DOES poll four *other* endpoints (summary/events/jobs/failures)
every real 30 seconds (``POLL_INTERVAL_MS``, its own deliberately-tuned
constant) via a real ``setInterval``. Section 42 of this task invites
extending Operations with TWS status -- wiring a fresh connect/
disconnect probe into that already-polled surface, the way ``get_tws_
status`` worked in Phase 1, would create exactly the every-30-seconds
churn this correction exists to preempt, even though nothing deployed
today actually does that yet. Fixed here regardless of which endpoint
eventually exposes TWS health, since the underlying pattern (a fresh
socket per call) was always the real problem, not a specific route.

``TwsHealthProbe`` fixes this the way Section 11 itself prescribes: one
persistent ``TWSConnectionManager``, reused for every call once READY
(a pure attribute read via ``health_snapshot()`` -- no I/O at all), with
a bounded minimum retry interval so a Gateway that is genuinely down
isn't hammered by 30-second UI polling either. Owned by the FastAPI app
lifecycle (``api/main.py``'s ``lifespan()``), mirroring this project's
existing precedent for the embedder/scheduler singletons -- one real
ownership model, not a fresh connection manufactured per request
(Section 41).

This probe is deliberately separate from whatever connection an actual
``IBKRTWSProvider`` uses to serve real market data (Section 9's own
concern) -- health-checking and data-serving are different real
responsibilities with different lifecycles; conflating them would mean
a health check could disrupt an in-flight quote request, or vice versa.
"""

import logging
import threading
import time

from providers.ibkr_client import IBKRError
from providers.ibkr_tws_client import TWSConnectionManager, TWSConnectionState, TWSHealthSnapshot

logger = logging.getLogger(__name__)

# Never hammer a genuinely-down Gateway/TWS every 30s just because
# Operations happens to poll that often -- a real, disclosed floor on
# how often a fresh connect attempt may be made after a failure.
DEFAULT_MIN_RETRY_INTERVAL_SECONDS = 30.0

# A fixed, deterministic offset from the real data-serving connection's
# own client id (core.config.Settings.ibkr_tws_client_id) -- Section 11's
# deterministic-client-id rule applies to this probe too; it must never
# collide with (or be mistaken for) the real connection a provider might
# also be holding open. Shared by api/main.py (constructs the probe) and
# services/system_status.py (Phase 1's own standalone one-shot fallback,
# used when no app-owned probe exists).
HEALTHCHECK_CLIENT_ID_OFFSET = 900


class TwsHealthProbe:
    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        min_retry_interval_seconds: float = DEFAULT_MIN_RETRY_INTERVAL_SECONDS,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._min_retry_interval = min_retry_interval_seconds
        self._connection: TWSConnectionManager | None = None
        self._last_attempt_at: float | None = None
        self._lock = threading.Lock()

    def snapshot(self) -> TWSHealthSnapshot:
        """Cheap (no I/O) when the held connection is already READY --
        the common case once a real Gateway has been reached once. Only
        attempts a real connect when there is no connection yet, or the
        held one isn't READY and the retry floor has elapsed."""
        with self._lock:
            if self._connection is not None and self._connection.state == TWSConnectionState.READY:
                return self._connection.health_snapshot()

            now = time.monotonic()
            if (
                self._last_attempt_at is not None
                and (now - self._last_attempt_at) < self._min_retry_interval
            ):
                # Too soon to retry -- report the real, held state
                # honestly rather than opening a new socket just because
                # a poll happened to land inside the backoff window.
                if self._connection is not None:
                    return self._connection.health_snapshot()
                return TWSHealthSnapshot(
                    provider="tws",
                    gateway_reachable=False,
                    socket_connected=False,
                    api_ready=False,
                    market_data_quality_last_seen=None,
                    last_heartbeat=None,
                    last_error="not yet attempted (within retry backoff)",
                    reconnect_state=TWSConnectionState.DISCONNECTED.value,
                )

            self._last_attempt_at = now
            if self._connection is None:
                self._connection = TWSConnectionManager(
                    host=self._host, port=self._port, client_id=self._client_id
                )
            try:
                self._connection.connect_and_start()
            except IBKRError as exc:
                logger.info("TWS health probe connect attempt failed: %s", exc)
            return self._connection.health_snapshot()

    def shutdown(self) -> None:
        """FastAPI lifespan teardown -- the one place this probe's
        connection is deliberately closed, mirroring the scheduler's own
        shutdown precedent in api/main.py."""
        with self._lock:
            if self._connection is not None:
                self._connection.shutdown()
                self._connection = None
