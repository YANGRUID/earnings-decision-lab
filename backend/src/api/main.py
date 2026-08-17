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
from api.routers import companies, earnings, health, options, research
from core.config import get_settings
from observability.logging import configure_logging
from rag.embeddings import FastEmbedProvider
from services.llm.factory import get_llm_provider

RESEARCH_QUERY_RATE_LIMIT = 10  # per window — real LLM cost per call, see api/rate_limit.py
RESEARCH_QUERY_RATE_WINDOW_SECONDS = 60.0

log = logging.getLogger("api.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    # Constructed once at startup: the LLM provider's httpx.Client and the
    # local embedding model are both safe/cheap to share across requests,
    # expensive to rebuild per-request. Neither failing to construct should
    # take down the whole app — /health, /companies, /earnings, and the
    # pure options calculators don't need either. AI-dependent endpoints
    # (api/deps.get_llm / get_embedder) raise a clear 503 if the one they
    # need wasn't available at startup, instead of the process refusing to
    # start at all. This is what let CI (which sets no real LLM key, by
    # design — see docs/llm_providers.md) run the API test suite at all.
    try:
        app.state.llm = get_llm_provider(settings)
    except Exception:
        log.warning(
            "LLM provider unavailable at startup; AI endpoints will return 503", exc_info=True
        )
        app.state.llm = None

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
    yield


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
        allow_origins=["http://localhost:5173", "http://localhost:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(companies.router, prefix="/api/v1")
    app.include_router(earnings.router, prefix="/api/v1")
    app.include_router(options.router, prefix="/api/v1")
    app.include_router(research.router, prefix="/api/v1")

    return app


app = create_app()
