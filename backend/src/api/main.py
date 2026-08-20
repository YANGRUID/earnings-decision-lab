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
    companies,
    decision_snapshots,
    earnings,
    earnings_calendar,
    evaluations,
    health,
    options,
    portfolio,
    provider_settings,
    replay,
    research,
    system_status,
    usage,
)
from core.config import get_settings
from observability.logging import configure_logging
from rag.embeddings import FastEmbedProvider
from services.scheduler import build_scheduler

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

    # Phase 4.2 -- the first real background scheduler in this codebase;
    # see services/scheduler.py for the full reasoning. Same resilience
    # posture as the embedder above: a scheduler that fails to start
    # (e.g. Postgres briefly unreachable at this exact moment) must not
    # take the whole API down with it -- every other endpoint is
    # unaffected by the earnings calendar sync being unavailable.
    try:
        app.state.scheduler = build_scheduler()
        app.state.scheduler.start()
    except Exception:
        log.warning("Scheduler failed to start; earnings calendar sync disabled", exc_info=True)
        app.state.scheduler = None

    yield

    if app.state.scheduler is not None:
        app.state.scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Earnings Decision Lab API",
        description=(
            "AI-assisted earnings intelligence, options analytics, and historical event research."
        ),
        version="0.1.0",
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
    app.include_router(decision_snapshots.router, prefix="/api/v1")
    app.include_router(options.router, prefix="/api/v1")
    app.include_router(research.router, prefix="/api/v1")
    app.include_router(evaluations.router, prefix="/api/v1")
    app.include_router(replay.router, prefix="/api/v1")
    app.include_router(system_status.router, prefix="/api/v1")
    app.include_router(portfolio.router, prefix="/api/v1")
    app.include_router(provider_settings.router, prefix="/api/v1")
    app.include_router(usage.router, prefix="/api/v1")

    return app


app = create_app()
