from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from agents.orchestrator import AgentOrchestrator
from core.config import get_settings
from db.session import SessionLocal
from rag.embeddings import EmbeddingProvider
from services.llm.base import LLMProvider
from services.llm.errors import LLMError, MissingAPIKeyError, UnknownProviderError
from services.llm.factory import get_llm_provider
from services.provider_settings import get_app_provider_settings


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
        return get_llm_provider(
            get_settings(),
            override_provider=overrides.llm_provider,
            override_model=overrides.llm_model,
        )
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


LLM = Annotated[LLMProvider, Depends(get_llm)]
Embedder = Annotated[EmbeddingProvider, Depends(get_embedder)]


def get_orchestrator(db: DbSession, llm: LLM, embedder: Embedder) -> AgentOrchestrator:
    return AgentOrchestrator(db, llm, embedder)


Orchestrator = Annotated[AgentOrchestrator, Depends(get_orchestrator)]
