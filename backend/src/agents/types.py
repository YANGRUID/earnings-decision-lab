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
    question: str
    answer: str
    citations: list[Citation] = field(default_factory=list)
    trace: ExecutionTrace | None = None
