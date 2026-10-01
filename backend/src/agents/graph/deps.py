"""Everything a node needs that is NOT part of the checkpointed state.

The split matters. ``ResearchState`` is what a checkpoint holds, so it must
stay JSON-safe and must not contain a database session, an HTTP client or a
loaded embedding model. ``GraphDeps`` holds exactly those, is constructed
per request (like the legacy orchestrator, which is also built per request
from db + llm + embedder), and is bound to the nodes when the graph is
compiled.

``outcomes`` is a per-run, in-memory side table of full ``ToolOutcome``
objects, keyed by (retrieval round, tool, arguments). It exists because
``merge_evidence`` needs real ``Citation`` objects and raw payloads while
state must stay serialisable. It is deliberately NOT checkpointed, and
``merge_evidence`` carries forward the already-checkpointed evidence block
when an outcome is absent -- which is exactly the resume case.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from agents.adapters.model import EDLChatModel
from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from rag.embeddings import EmbeddingProvider


@dataclass
class GraphDeps:
    db: Session
    model: EDLChatModel
    embedder: EmbeddingProvider
    #: The underlying tool registry. Kept alongside the LangChain-adapted
    #: tools because the deterministic argument defaulting needs each
    #: tool's own ``args_schema.model_fields`` (agents/evidence.py).
    registry: dict[str, Tool[Any]]
    outcomes: dict[str, ToolOutcome] = field(default_factory=dict)
