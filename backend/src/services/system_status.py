"""Real counts, freshness timestamps, and configuration status for the
Data/Evaluation Status page -- every value here is a direct query against
this deployment's own database or settings, never an assumption about what
should be there.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.config import Settings
from models.company import Company
from models.document_chunk import DocumentChunk
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.filing import Filing
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.volatility_snapshot import VolatilitySnapshot
from providers.ibkr_client import IBKRClient, IBKRError
from providers.ibkr_tws_client import TWSConnectionManager, TWSConnectionState
from providers.ibkr_tws_health import HEALTHCHECK_CLIENT_ID_OFFSET, TwsHealthProbe
from providers.ibkr_tws_options import IBKRTWSProvider

# Provider -> the Settings field names required to actually run it, mirroring
# services/llm/factory.py's own checks without instantiating a real client
# (this page must not require a working API key just to be viewed).
_LLM_PROVIDER_REQUIRED_SETTINGS: dict[str, tuple[str, str]] = {
    "deepseek": ("deepseek_api_key", "deepseek_model"),
    "openai": ("openai_api_key", "openai_model"),
    "anthropic": ("anthropic_api_key", "anthropic_model"),
    "openai_compatible": ("openai_compatible_api_key", "openai_compatible_model"),
}


@dataclass(frozen=True)
class DataCounts:
    companies: int
    earnings_events: int
    earnings_events_with_results: int
    price_bars: int
    filings: int
    document_chunks: int
    earnings_estimate_snapshots: int
    options_snapshots: int
    volatility_snapshots: int


def get_data_counts(db: Session) -> DataCounts:
    return DataCounts(
        companies=db.query(func.count(Company.id)).scalar() or 0,
        earnings_events=db.query(func.count(EarningsEvent.id)).scalar() or 0,
        earnings_events_with_results=db.query(func.count(EarningsResult.id)).scalar() or 0,
        price_bars=db.query(func.count(PriceBar.id)).scalar() or 0,
        filings=db.query(func.count(Filing.id)).scalar() or 0,
        document_chunks=db.query(func.count(DocumentChunk.id)).scalar() or 0,
        earnings_estimate_snapshots=db.query(func.count(EarningsEstimateSnapshot.id)).scalar() or 0,
        options_snapshots=db.query(func.count(OptionsSnapshot.id)).scalar() or 0,
        volatility_snapshots=db.query(func.count(VolatilitySnapshot.id)).scalar() or 0,
    )


@dataclass(frozen=True)
class DataFreshness:
    latest_price_bar_date: date | None
    latest_filing_retrieved_at: datetime | None
    latest_earnings_estimate_snapshot_at: datetime | None
    latest_options_snapshot_at: datetime | None


def get_data_freshness(db: Session) -> DataFreshness:
    return DataFreshness(
        latest_price_bar_date=db.query(func.max(PriceBar.trade_date)).scalar(),
        latest_filing_retrieved_at=db.query(func.max(Filing.retrieved_at)).scalar(),
        latest_earnings_estimate_snapshot_at=db.query(
            func.max(EarningsEstimateSnapshot.snapshot_timestamp)
        ).scalar(),
        latest_options_snapshot_at=db.query(func.max(OptionsSnapshot.snapshot_timestamp)).scalar(),
    )


@dataclass(frozen=True)
class LlmConfigStatus:
    provider: str
    model: str | None
    configured: bool


def describe_llm_configuration(settings: Settings) -> LlmConfigStatus:
    """Reports what's configured without instantiating a real provider
    client -- this page must render even when no API key is set or the key
    is invalid, since "not configured" is itself real status information.
    """
    provider = settings.llm_provider.lower()
    required = _LLM_PROVIDER_REQUIRED_SETTINGS.get(provider)
    if required is None:
        return LlmConfigStatus(provider=provider, model=None, configured=False)

    api_key_field, model_field = required
    configured = bool(getattr(settings, api_key_field)) and bool(getattr(settings, model_field))
    model = getattr(settings, model_field)
    return LlmConfigStatus(provider=provider, model=model, configured=configured)


@dataclass(frozen=True)
class IbkrStatus:
    gateway_reachable: bool
    authenticated: bool
    connected: bool
    competing: bool
    error: str | None
    # Phase 4.8A -- the short, glanceable label the runtime-automation
    # brief asks for ("IBKR: CONNECTED" / "IBKR: AUTH_REQUIRED"), computed
    # from the same real fields above by ibkr_status_label() below. Not a
    # second live check -- every existing field on this dataclass is
    # unchanged, so frontend/src/pages/Settings/Ibkr.tsx's existing
    # consumption of gateway_reachable/authenticated/connected/competing
    # keeps working exactly as before; this is purely additive.
    status_label: str


def ibkr_status_label(
    *, gateway_reachable: bool, authenticated: bool, connected: bool, competing: bool
) -> str:
    """Pure mapping, no I/O -- kept separate from get_ibkr_status() so the
    keep-alive scheduler job (services/scheduler.py::run_ibkr_gateway_
    healthcheck_job) can log the same real label the status page shows,
    without a second live call. Order matters: a competing session is
    reported even when authenticated=True (mirrors IBKRClient.ensure_
    authenticated()'s own precedence -- see providers/ibkr_client.py)."""
    if not gateway_reachable:
        return "GATEWAY_UNREACHABLE"
    if competing:
        return "COMPETING_SESSION"
    if not (authenticated and connected):
        return "AUTH_REQUIRED"
    return "CONNECTED"


def get_ibkr_status(settings: Settings) -> IbkrStatus:
    """A real, live check against the Gateway's own /iserver/auth/status --
    never cached, never assumed. This page is exactly the place a live
    check belongs (visited deliberately, not on every research action);
    Strategy Lab and other research pages read persisted OptionsSnapshot
    rows instead, never blocking on a live Gateway round-trip."""
    client = IBKRClient(base_url=settings.ibkr_base_url)
    try:
        status = client.auth_status()
        return IbkrStatus(
            gateway_reachable=True,
            authenticated=status.authenticated,
            connected=status.connected,
            competing=status.competing,
            error=None,
            status_label=ibkr_status_label(
                gateway_reachable=True,
                authenticated=status.authenticated,
                connected=status.connected,
                competing=status.competing,
            ),
        )
    except IBKRError as exc:
        return IbkrStatus(
            gateway_reachable=False,
            authenticated=False,
            connected=False,
            competing=False,
            error=str(exc),
            status_label=ibkr_status_label(
                gateway_reachable=False, authenticated=False, connected=False, competing=False
            ),
        )


# IBKR TWS Migration Phase 1, Section 35 -- a distinct, additive status
# type for the new TWS transport, alongside IbkrStatus above (the existing
# Web/Client-Portal-Gateway status, UNCHANGED by this migration).
_TWS_HEALTHCHECK_CONNECT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class TwsStatus:
    configured: bool  # settings.ibkr_provider == "tws"
    gateway_reachable: bool
    socket_connected: bool
    api_ready: bool
    market_data_quality: str | None
    error: str | None
    status_label: str
    # IBKR TWS Migration, Phase 3 readiness (Section 21/42) -- additive,
    # already computed server-side on TWSHealthSnapshot/TWSConnectionState
    # and simply not threaded through this response before now. Never a
    # new capability: last_heartbeat is None until a real callback has
    # ever landed; reconnect_state is TWSConnectionState's own real value
    # ("disconnected" | "connecting" | "connected" | "ready" |
    # "reconnecting" | "failed"), distinct from and more granular than
    # status_label above (which only distinguishes the 4 coarse buckets a
    # Settings page needs for its headline state).
    last_heartbeat: datetime | None
    reconnect_state: str


def tws_status_label(*, configured: bool, gateway_reachable: bool, api_ready: bool) -> str:
    """Pure mapping, no I/O -- mirrors ibkr_status_label's own precedent.
    Deliberately distinguishes NOT_CONFIGURED (ibkr_provider != "tws",
    the real default -- see core/config.py) from GATEWAY_UNREACHABLE (tws
    selected but no real IB Gateway/TWS process answers) and AUTH_REQUIRED
    (Section 37 -- a real socket connection was made, but no nextValidId
    ever arrived, which for IB Gateway/TWS means it isn't logged into the
    brokerage session yet -- manual authentication required, never a
    generic "IBKR down")."""
    if not configured:
        return "NOT_CONFIGURED"
    if not gateway_reachable:
        return "GATEWAY_UNREACHABLE"
    if not api_ready:
        return "AUTH_REQUIRED"
    return "CONNECTED"


def _status_from_snapshot(snapshot, provider_quality: str | None = None) -> TwsStatus:  # noqa: ANN001 -- TWSHealthSnapshot, avoids an import cycle note below
    return TwsStatus(
        configured=True,
        gateway_reachable=snapshot.gateway_reachable,
        socket_connected=snapshot.socket_connected,
        api_ready=snapshot.api_ready,
        # Provider-observed quality wins when this snapshot's own
        # connection has never seen a marketDataType callback -- see
        # get_tws_status's own docstring for the real reason.
        market_data_quality=snapshot.market_data_quality_last_seen or provider_quality,
        error=snapshot.last_error if not snapshot.api_ready else None,
        status_label=tws_status_label(
            configured=True,
            gateway_reachable=snapshot.gateway_reachable,
            api_ready=snapshot.api_ready,
        ),
        last_heartbeat=snapshot.last_heartbeat,
        reconnect_state=snapshot.reconnect_state,
    )


def get_tws_status(
    settings: Settings,
    probe: TwsHealthProbe | None = None,
    provider: IBKRTWSProvider | None = None,
) -> TwsStatus:
    """IBKR TWS Migration Phase 2, Section 11 -- prefers ``probe``, a
    persistent ``TwsHealthProbe`` owned by the FastAPI app lifecycle
    (api/main.py), which reuses one long-lived connection and is a cheap
    attribute read once READY -- never a fresh connect/disconnect per
    call. ``probe=None`` (a standalone script with no app lifecycle to
    own one, e.g. scripts/ibkr_provider_parity.py, or a test) falls back
    to Phase 1's original bounded one-shot connect -> probe -> disconnect
    behavior, preserved exactly for that real, narrower use case.

    Skips any real connection attempt entirely (no-op, ``configured=
    False``) unless ``ibkr_provider`` is actually "tws" -- mirrors
    run_ibkr_gateway_healthcheck_job's own existing skip-when-not-
    selected pattern for the Web provider (services/scheduler.py).

    ``provider`` (production cutover, 2026-09-01) is the shared, market-
    data-serving IBKRTWSProvider, when the caller has one. Real gap this
    cutover surfaced live: ``market_data_quality`` is only ever learned
    from a real ``marketDataType`` callback, and the health probe never
    requests market data at all -- it only connects -- so the probe's own
    connection reports ``None`` forever, and Operations/System Status
    showed "—" even though the delayed/live state was already known for
    certain on the production connection. Preferring the provider's own
    observed value here is what makes the DELAYED label honest and
    visible (never guessed: still ``None`` until a real callback has
    actually been seen on some connection)."""
    if settings.ibkr_provider.lower() != "tws":
        return TwsStatus(
            configured=False,
            gateway_reachable=False,
            socket_connected=False,
            api_ready=False,
            market_data_quality=None,
            error=None,
            status_label=tws_status_label(
                configured=False, gateway_reachable=False, api_ready=False
            ),
            last_heartbeat=None,
            reconnect_state=TWSConnectionState.DISCONNECTED.value,
        )

    provider_quality: str | None = None
    if provider is not None:
        provider_quality = provider._connection.health_snapshot().market_data_quality_last_seen  # noqa: SLF001

    if probe is not None:
        return _status_from_snapshot(probe.snapshot(), provider_quality)

    connection = TWSConnectionManager(
        host=settings.ibkr_tws_host,
        port=settings.ibkr_tws_port,
        client_id=settings.ibkr_tws_client_id + HEALTHCHECK_CLIENT_ID_OFFSET,
        connect_timeout=_TWS_HEALTHCHECK_CONNECT_TIMEOUT_SECONDS,
    )
    try:
        connection.connect_and_start()
        return _status_from_snapshot(connection.health_snapshot(), provider_quality)
    except IBKRError as exc:
        return TwsStatus(
            configured=True,
            gateway_reachable=False,
            socket_connected=False,
            api_ready=False,
            market_data_quality=None,
            error=str(exc),
            status_label=tws_status_label(
                configured=True, gateway_reachable=False, api_ready=False
            ),
            last_heartbeat=None,
            reconnect_state=connection.state.value,
        )
    finally:
        connection.shutdown()
