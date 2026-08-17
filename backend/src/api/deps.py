from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from agents.orchestrator import AgentOrchestrator
from db.session import SessionLocal
from rag.embeddings import EmbeddingProvider
from services.llm.base import LLMProvider
from services.llm.errors import LLMError


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_llm(request: Request) -> LLMProvider:
    llm = request.app.state.llm
    if llm is None:
        raise LLMError(
            "No LLM provider is configured (see docs/llm_providers.md) — AI research "
            "endpoints are unavailable, but data endpoints still work."
        )
    return llm


def get_embedder(request: Request) -> EmbeddingProvider:
    embedder = request.app.state.embedder
    if embedder is None:
        raise LLMError("The embedding model failed to load at startup — search is unavailable.")
    return embedder


DbSession = Annotated[Session, Depends(get_db)]
LLM = Annotated[LLMProvider, Depends(get_llm)]
Embedder = Annotated[EmbeddingProvider, Depends(get_embedder)]


def get_orchestrator(db: DbSession, llm: LLM, embedder: Embedder) -> AgentOrchestrator:
    return AgentOrchestrator(db, llm, embedder)


Orchestrator = Annotated[AgentOrchestrator, Depends(get_orchestrator)]
