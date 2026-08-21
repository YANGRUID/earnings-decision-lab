"""Phase 4.8A -- makes connecting to IBKR a first-class in-app workflow
instead of "go find the Gateway's yaml/README and figure out the URL
yourself". This router does exactly one thing: hand the frontend the
real, current browser-facing IBKR Client Portal Gateway login URL so a
"Connect IBKR" button can open it.

Deliberately minimal, deliberately not more than this:
- No password field, no form, no session handling here. The Gateway
  itself (whichever one is configured -- the automated ibkr-gateway
  container, or a manually-run host Gateway) serves its own real login
  page; this project never renders, proxies, or intercepts it.
- No credentials are read, accepted, or stored by this endpoint or
  anywhere in this backend -- see core/config.py's ibkr_gateway_port
  and docs/ibkr_gateway_runtime.md for where the automated path's
  credentials actually live (the ibkr-gateway container's own
  environment, never this application).
- READ-ONLY, same guarantee as every other IBKR path in this project:
  no order-placement, modification, cancellation, or execution endpoint
  exists anywhere in this router, and none ever will.
"""

from fastapi import APIRouter

from core.config import get_settings
from schemas.api import IbkrConnectResponse

router = APIRouter(prefix="/ibkr", tags=["ibkr"])


@router.get("/connect", response_model=IbkrConnectResponse)
def get_ibkr_connect_url() -> IbkrConnectResponse:
    """The Gateway's own login page, reachable from the operator's own
    browser -- always `localhost`, never `host.docker.internal` (that
    hostname only resolves from inside another container, not from a
    browser on the host machine). Port comes from settings.
    ibkr_gateway_port (default 5000, matching docker-compose.yml's
    ibkr-gateway service and .env.example's IBKR_GATEWAY_PORT) so this
    stays correct if that port is ever remapped.
    """
    settings = get_settings()
    return IbkrConnectResponse(url=f"https://localhost:{settings.ibkr_gateway_port}")
