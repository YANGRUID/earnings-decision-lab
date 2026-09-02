from collections.abc import Generator
from typing import Annotated

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from agents.orchestrator import AgentOrchestrator
from core.config import get_settings
from db.session import SessionLocal
from providers.ibkr_tws_health import TwsHealthProbe
from providers.ibkr_tws_options import IBKRTWSProvider
from rag.embeddings import EmbeddingProvider
from services.llm.base import LLMProvider
from services.llm.errors import LLMError, MissingAPIKeyError, UnknownProviderError
from services.llm.factory import get_llm_provider
from services.provider_settings import get_app_provider_settings
from services.usage_instrumentation import InstrumentedLLMProvider


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]


def get_llm(db: DbSession) -> LLMProvider:
    """Constructed fresh on every request from the current owner override
    (see services/provider_settings.py) -- never a startup-time singleton,
    so changing the LLM provider in the Settings UI takes effect on the
    very next AI Research call, not after a restart. Cheap: an LLMProvider
    is just a thin httpx client wrapper, not a loaded model.
    """
    overrides = get_app_provider_settings(db)
    try:
        provider = get_llm_provider(
            get_settings(),
            override_provider=overrides.llm_provider,
            override_model=overrides.llm_model,
            db=db,
        )
        return InstrumentedLLMProvider(provider, db, provider.name)
    except (MissingAPIKeyError, UnknownProviderError) as exc:
        raise LLMError(
            f"No LLM provider is configured or reachable ({exc}) — see "
            "docs/llm_providers.md or Settings > AI Provider. AI research endpoints are "
            "unavailable, but data endpoints still work."
        ) from exc


def get_embedder(request: Request) -> EmbeddingProvider:
    embedder = request.app.state.embedder
    if embedder is None:
        raise LLMError("The embedding model failed to load at startup — search is unavailable.")
    return embedder


def get_scheduler(request: Request) -> AsyncIOScheduler | None:
    """Phase 4.9 -- unlike get_embedder above, this never raises: a
    scheduler that failed to start (see api/main.py's lifespan()) is
    real, reportable status for GET /system-status
    (services/scheduler.py::get_scheduler_status already handles a None
    scheduler honestly), not a request failure."""
    return request.app.state.scheduler


def get_tws_health_probe(request: Request) -> TwsHealthProbe | None:
    """IBKR TWS Migration Phase 2, Section 11 -- the one persistent TWS
    health-check connection, owned by api/main.py's lifespan(). None
    when ibkr_provider != "tws" (the real, current default) -- never
    raises, mirrors get_scheduler's own honest-None precedent above;
    services/system_status.py::get_tws_status handles a None probe by
    falling back to Phase 1's original bounded one-shot probe."""
    return request.app.state.tws_health_probe


def get_tws_provider(request: Request) -> IBKRTWSProvider | None:
    """IBKR TWS Migration, production cutover (2026-09-01) -- the ONE
    long-lived, market-data-serving IBKRTWSProvider owned by
    api/main.py's lifespan() (distinct from get_tws_health_probe above,
    which owns a separate connection at a separate client id, purely for
    health checks). None when ibkr_provider != "tws" -- never raises,
    same honest-None precedent as get_scheduler/get_tws_health_probe.

    Exists so a caller INSIDE this FastAPI process can reach the real
    shared singleton. A separate process (a script, a `docker compose
    exec python ...`) importing providers/factory.py does NOT share this
    -- its module-level _shared_tws_provider is None there, so it would
    silently construct a SECOND connection at the same client id and
    collide with this one (confirmed live during this cutover)."""
    return request.app.state.tws_provider


LLM = Annotated[LLMProvider, Depends(get_llm)]
Embedder = Annotated[EmbeddingProvider, Depends(get_embedder)]
Scheduler = Annotated[AsyncIOScheduler | None, Depends(get_scheduler)]
TwsHealthProbeDep = Annotated[TwsHealthProbe | None, Depends(get_tws_health_probe)]
TwsProviderDep = Annotated[IBKRTWSProvider | None, Depends(get_tws_provider)]


def get_orchestrator(db: DbSession, llm: LLM, embedder: Embedder) -> AgentOrchestrator:
    return AgentOrchestrator(db, llm, embedder)


Orchestrator = Annotated[AgentOrchestrator, Depends(get_orchestrator)]
