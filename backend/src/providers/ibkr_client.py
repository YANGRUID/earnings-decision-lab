"""Low-level HTTP client for the Interactive Brokers Client Portal Gateway
-- a LOCAL process the user runs and authenticates on their own machine
(see docs/ibkr_integration.md). This project never sees, requests, or
stores an IBKR username, password, 2FA code, or session cookie;
authentication happens entirely inside the Gateway, outside this codebase,
before this client ever makes a request.

READ ONLY: no method anywhere in the IBKR integration calls an order
placement, order modification, order cancellation, order preview, what-if,
or exercise endpoint, and none ever will -- see docs/ibkr_integration.md.

TLS verification is disabled ONLY for this client, scoped to exactly this
one httpx.Client instance -- the Gateway serves a local, self-signed
certificate for https://localhost, which is expected, documented IBKR
behavior for local development (not a real trust concern for a
loopback-only connection) and is never extended to any other HTTP client in
this codebase.
"""

from dataclasses import dataclass

import httpx

from observability.http_client import new_http_client


class IBKRError(Exception):
    """Base class for all IBKR integration errors."""


class IBKRGatewayUnavailableError(IBKRError):
    """The Gateway process isn't reachable at all (not running, wrong port,
    network/timeout error) -- distinct from "running but not
    authenticated"."""


class IBKRNotAuthenticatedError(IBKRError):
    """The Gateway is reachable but the brokerage session isn't
    authenticated/connected -- the user needs to log in at the Gateway's
    own web page; this project cannot and does not do that on their
    behalf."""


class IBKRCompetingSessionError(IBKRError):
    """Another session (e.g. TWS, another browser tab) currently holds the
    brokerage connection -- IBKR allows only one active session per
    account."""


class IBKRRateLimitedError(IBKRError):
    """The Gateway returned a rate-limit response."""


@dataclass(frozen=True)
class AuthStatus:
    authenticated: bool
    connected: bool
    competing: bool


def decode_market_data_quality(availability_code: str | None) -> str:
    """Decodes IBKR's field 6509 (Market Data Availability) first
    character -- the only part of the code this project treats as
    authoritative, since IBKR's own documentation confirms its meaning
    precisely: R=RealTime, D=Delayed, Z=Frozen, Y=Frozen Delayed,
    N=Not Subscribed (see docs/ibkr_integration.md for the citation). The
    second/third characters observed live on real responses (e.g. "ZBd" on
    real NVDA option snapshots) aren't documented with enough confidence in
    any source this project could verify, so they're deliberately not
    decoded further rather than guessed. Returns "unknown" for a
    missing/unrecognized code.
    """
    if not availability_code:
        return "unknown"
    first = availability_code[0]
    if first == "R":
        return "live"
    if first == "D":
        return "delayed"
    if first in ("Z", "Y"):
        return "frozen"
    if first == "N":
        return "unavailable"
    return "unknown"


def mask_account_id(account_id: str) -> str:
    """First 3 and last 2 characters only, e.g. "U12345678" -> "U12****78"
    -- matches the exact masking convention the user specified. The real
    account ID must never be logged, persisted, or included in any report;
    every caller that touches an account ID from the Gateway masks it
    immediately, before it's used anywhere else.
    """
    if len(account_id) <= 5:
        return "*" * len(account_id)
    return f"{account_id[:3]}****{account_id[-2:]}"


class IBKRClient:
    """Thin wrapper around the Gateway's REST API. Translates
    connection-level failures into IBKRError subclasses so callers only
    ever need to handle one exception family, never raw httpx exceptions.
    """

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._client = client or new_http_client(base_url=base_url, verify=False, timeout=20.0)

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        try:
            response = self._client.get(path, params=params)
        except httpx.ConnectError as exc:
            raise IBKRGatewayUnavailableError(
                f"could not reach the IBKR Client Portal Gateway at "
                f"{self._client.base_url} -- is it running? See "
                "docs/ibkr_integration.md."
            ) from exc
        except httpx.TimeoutException as exc:
            raise IBKRGatewayUnavailableError(
                f"IBKR Client Portal Gateway request to {path} timed out"
            ) from exc

        if response.status_code == 429:
            raise IBKRRateLimitedError(
                f"IBKR Client Portal Gateway rate-limited the request to {path}"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise IBKRError(
                f"IBKR Client Portal Gateway returned {response.status_code} for {path}: "
                f"{response.text[:300]}"
            ) from exc
        return response

    def auth_status(self) -> AuthStatus:
        data = self.get("/iserver/auth/status").json()
        return AuthStatus(
            authenticated=bool(data.get("authenticated")),
            connected=bool(data.get("connected")),
            competing=bool(data.get("competing")),
        )

    def ensure_authenticated(self) -> AuthStatus:
        """Raises a specific IBKRError subclass for every non-usable
        session state; returns the real status only when the session is
        genuinely usable for read-only requests.
        """
        status = self.auth_status()
        if status.competing:
            raise IBKRCompetingSessionError(
                "another brokerage session is currently active for this IBKR account "
                "(competing session) -- only one session can hold the connection at a time"
            )
        if not (status.authenticated and status.connected):
            raise IBKRNotAuthenticatedError(
                "IBKR Client Portal Gateway is reachable but the brokerage session isn't "
                "authenticated -- log in at the Gateway's own web page, then retry"
            )
        return status
