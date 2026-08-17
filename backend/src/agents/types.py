from dataclasses import dataclass, field
from decimal import Decimal

from rag.context import Citation


@dataclass(frozen=True)
class ToolCallRecord:
    tool_name: str
    arguments: dict
    success: bool
    duration_ms: float
    summary: str
    error: str | None = None
    query_description: str | None = None


@dataclass(frozen=True)
class ExecutionTrace:
    intent_category: str
    planning_method: str  # "native_tool_calling" | "structured_planner"
    tool_calls: list[ToolCallRecord]
    verification_ran: bool
    verification_supported: bool | None
    revised: bool
    model: str
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: Decimal | None
    total_duration_ms: float


@dataclass(frozen=True)
class AgentResponse:
    """``trace`` is required, not optional: AgentOrchestrator.run() is the
    only place this is constructed, and it always builds a real
    ExecutionTrace (see agents/orchestrator.py) — even a request that fails
    at every stage still gets a trace describing that. Making it Optional
    understated a real guarantee and forced every consumer (the /research/query
    router) to null-check a value that's never actually null.
    """

    question: str
    answer: str
    trace: ExecutionTrace
    citations: list[Citation] = field(default_factory=list)
