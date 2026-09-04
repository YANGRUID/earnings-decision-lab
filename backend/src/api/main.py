"""FastAPI application entry point.

Run locally: uv run uvicorn api.main:app --reload --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.exceptions import register_exception_handlers
from api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from api.rate_limit import SlidingWindowRateLimiter
from api.routers import (
    admin,
    companies,
    earnings,
    earnings_calendar,
    evaluations,
    health,
    ibkr,
    operations,
    provider_settings,
    research,
    system_status,
    tws_diagnostics,
    usage,
    v4_experimental,
    v4_settlement_recovery,
    v4_shadow,
)
from core.config import get_settings
from observability.logging import configure_logging
from providers.factory import set_shared_tws_provider
from providers.ibkr_tws_health import HEALTHCHECK_CLIENT_ID_OFFSET, TwsHealthProbe
from providers.ibkr_tws_options import IBKRTWSProvider
from rag.embeddings import FastEmbedProvider
from services.scheduler import build_scheduler, retire_stale_jobs

RESEARCH_QUERY_RATE_LIMIT = 10  # per window — real LLM cost per call, see api/rate_limit.py
RESEARCH_QUERY_RATE_WINDOW_SECONDS = 60.0

log = logging.getLogger("api.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    # The local embedding model is safe/cheap to share across requests,
    # expensive to reload per-request -- constructed once at startup.
    # Failing to construct shouldn't take down the whole app — /health,
    # /companies, /earnings, and the pure options calculators don't need
    # it. RAG-dependent endpoints (api/deps.get_embedder) raise a clear
    # 503 if it wasn't available, instead of the process refusing to start
    # at all. This is what let CI (which sets no real LLM key, by design —
    # see docs/llm_providers.md) run the API test suite at all.
    #
    # The LLM provider, by contrast, is deliberately NOT constructed here
    # -- api/deps.get_llm builds it fresh on every request from the
    # current owner-configured override (see services/provider_settings.py),
    # so switching providers in the Settings UI takes effect immediately
    # rather than requiring a restart.
    try:
        app.state.embedder = FastEmbedProvider()
    except Exception:
        log.warning(
            "Embedding model unavailable at startup; RAG endpoints will return 503", exc_info=True
        )
        app.state.embedder = None

    app.state.research_rate_limiter = SlidingWindowRateLimiter(
        max_requests=RESEARCH_QUERY_RATE_LIMIT, window_seconds=RESEARCH_QUERY_RATE_WINDOW_SECONDS
    )

    # IBKR TWS Migration Phase 2, Section 11 -- one persistent TWS health
    # probe for the whole app lifetime, never a fresh connect/disconnect
    # per request (see providers/ibkr_tws_health.py's own module
    # docstring for the real churn risk this preempts). Only constructed
    # when TWS is actually selected -- the real, current default (Web)
    # never touches this at all, matching this migration's own Section 9
    # "no automatic switch" rule. Deliberately a SEPARATE connection from
    # whatever an IBKRTWSProvider instance uses to serve real market data
    # -- health-checking and data-serving are different lifecycles.
    # Built BEFORE build_scheduler() below (Phase 3 readiness) so the
    # same shared instance can be handed to the scheduler for its own
    # provider-aware healthcheck job -- see run_ibkr_gateway_healthcheck_
    # job's own docstring for why it must reuse this exact probe rather
    # than opening a second, independent TWS connection.
    if settings.ibkr_provider.lower() == "tws":
        app.state.tws_health_probe = TwsHealthProbe(
            host=settings.ibkr_tws_host,
            port=settings.ibkr_tws_port,
            client_id=settings.ibkr_tws_client_id + HEALTHCHECK_CLIENT_ID_OFFSET,
        )
    else:
        app.state.tws_health_probe = None

    # IBKR TWS Migration, Phase 3 readiness (Section 5) -- the ONE
    # production TWS connection this backend process owns for actually
    # serving market data (distinct from tws_health_probe above, which
    # only ever reads a health snapshot). Client id is settings.ibkr_tws_
    # client_id itself (the plain backend default, e.g. 101) -- distinct
    # from the health probe's own +HEALTHCHECK_CLIENT_ID_OFFSET id and
    # from research-worker's own separate IBKR_TWS_CLIENT_ID override
    # (a different container/process entirely, see docker-compose.yml).
    # Constructing IBKRTWSProvider here does not itself open a socket --
    # like TwsHealthProbe, the real connect happens lazily, on first real
    # use (TWSConnectionManager.ensure_connected(), called by every
    # provider method) -- so this never blocks or risks failing startup.
    # set_shared_tws_provider makes this the instance providers/
    # factory.py::_build_ibkr_transport hands back to every real in-
    # process caller (the scheduler's decision/entry/exit-capture jobs,
    # services/options_reconstruction.py, services/research_
    # orchestration.py, ...) instead of each constructing -- and never
    # cleanly closing -- its own socket under the identical client id;
    # see that function's own docstring for the full rationale.
    if settings.ibkr_provider.lower() == "tws":
        app.state.tws_provider = IBKRTWSProvider(
            host=settings.ibkr_tws_host,
            port=settings.ibkr_tws_port,
            client_id=settings.ibkr_tws_client_id,
        )
        set_shared_tws_provider(app.state.tws_provider)
    else:
        app.state.tws_provider = None

    # Phase 4.2 -- the first real background scheduler in this codebase;
    # see services/scheduler.py for the full reasoning. Same resilience
    # posture as the embedder above: a scheduler that fails to start
    # (e.g. Postgres briefly unreachable at this exact moment) must not
    # take the whole API down with it -- every other endpoint is
    # unaffected by the earnings calendar sync being unavailable.
    try:
        app.state.scheduler = build_scheduler(
            embedder=app.state.embedder,
            tws_health_probe=app.state.tws_health_probe,
        )
        app.state.scheduler.start()
        retire_stale_jobs(app.state.scheduler)
    except Exception:
        log.warning("Scheduler failed to start; earnings calendar sync disabled", exc_info=True)
        app.state.scheduler = None

    yield

    if app.state.scheduler is not None:
        app.state.scheduler.shutdown(wait=False)
    if app.state.tws_health_probe is not None:
        app.state.tws_health_probe.shutdown()
    if app.state.tws_provider is not None:
        app.state.tws_provider.shutdown()
        set_shared_tws_provider(None)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Earnings Decision Lab API",
        description=(
            "AI-assisted earnings intelligence, options analytics, and historical event research."
        ),
        version="4.0.2",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        # Any localhost port, not a fixed list -- this project's frontend
        # gets started from several different tools at several different
        # ports (Vite's own :5173 default, CRA-style :3000, the Playwright
        # E2E suite's dedicated :5180, and an editor/agent preview server
        # that assigns whatever port happens to be free), and a real,
        # observed failure mode is that server starting fine while every
        # API call silently fails CORS because its port wasn't yet in a
        # hardcoded list. Still origin-restricted to localhost only, never
        # a public wildcard.
        allow_origin_regex=r"http://localhost:\d+",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(companies.router, prefix="/api/v1")
    app.include_router(earnings.router, prefix="/api/v1")
    app.include_router(earnings_calendar.router, prefix="/api/v1")
    app.include_router(ibkr.router, prefix="/api/v1")
    app.include_router(research.router, prefix="/api/v1")
    app.include_router(evaluations.router, prefix="/api/v1")
    app.include_router(system_status.router, prefix="/api/v1")
    app.include_router(operations.router, prefix="/api/v1")
    app.include_router(provider_settings.router, prefix="/api/v1")
    app.include_router(usage.router, prefix="/api/v1")
    # IBKR TWS Migration -- one read-only, in-process diagnostic (see
    # api/routers/tws_diagnostics.py's own docstring for why verifying
    # the shared TWS connection cannot be done from a separate process).
    # No DB session, no credentials, no order path. Post-cutover cleanup
    # (2026-09-01): opt-in ONLY -- not registered at all unless
    # ENABLE_INTERNAL_DIAGNOSTICS is explicitly true, so the normal
    # production API surface neither exposes nor documents it.
    if settings.enable_internal_diagnostics:
        app.include_router(tws_diagnostics.router, prefix="/api/v1")
        # The one write-capable operator route (end-of-day settlement
        # recovery), kept in its own module so tws_diagnostics stays
        # read-only by construction. Still a dry run unless the caller
        # passes confirm=APPEND.
        app.include_router(v4_settlement_recovery.router, prefix="/api/v1")
    # Phase 4.9 -- developer-only, on-demand triggers for the real
    # earnings pipeline jobs (see api/routers/admin.py). Registered at
    # all only outside production, so a production deployment doesn't
    # even list these routes in /docs -- not merely 404 them at call
    # time (admin.py's own _ensure_enabled() is a second, redundant
    # check, kept as defense in depth).
    if settings.app_env.lower() != "production":
        app.include_router(admin.router, prefix="/api/v1")
        # V4.4C -- READ-ONLY V4 shadow evidence inspection. Same
        # non-production guard as admin/v4-experimental above: V4
        # remains experimental, and this is a research surface. No
        # mutation endpoint exists (api/routers/v4_shadow.py).
        app.include_router(v4_shadow.router, prefix="/api/v1")
        # V4.2 (2026-09-01) -- experimental, read-only diagnostic only
        # (api/routers/v4_experimental.py's own docstring). Same
        # registration guard as admin.router immediately above: not
        # even listed in /docs for a production deployment.
        app.include_router(v4_experimental.router, prefix="/api/v1")

    return app


app = create_app()
