"""Tool output shape. Every tool returns one of these — the orchestrator
never inspects a tool's internal return type directly, so adding a new tool
never requires changing orchestrator code.
"""

from dataclasses import dataclass, field

from rag.context import Citation


@dataclass(frozen=True)
class ToolOutcome:
    success: bool
    summary: str  # short, LLM-context-ready description of what was found
    data: dict  # JSON-serializable structured result
    citations: list[Citation] = field(default_factory=list)
    query_description: str | None = None  # e.g. compiled SQL — safe to show a user
    error: str | None = None
