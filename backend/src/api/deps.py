from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from agents.orchestrator import AgentOrchestrator
from db.session import SessionLocal
from rag.embeddings import EmbeddingProvider
from services.llm.base import LLMProvider


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_llm(request: Request) -> LLMProvider:
    return request.app.state.llm


def get_embedder(request: Request) -> EmbeddingProvider:
    return request.app.state.embedder


DbSession = Annotated[Session, Depends(get_db)]
LLM = Annotated[LLMProvider, Depends(get_llm)]
Embedder = Annotated[EmbeddingProvider, Depends(get_embedder)]


def get_orchestrator(db: DbSession, llm: LLM, embedder: Embedder) -> AgentOrchestrator:
    return AgentOrchestrator(db, llm, embedder)


Orchestrator = Annotated[AgentOrchestrator, Depends(get_orchestrator)]
