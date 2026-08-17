"""Tool interface. Every agent tool wraps an already-real, already-tested
piece of this project (a DB query, the RAG pipeline, or the deterministic
options engine) — the agent layer adds orchestration on top of existing
functionality, it does not reimplement it.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel

from agents.tools.types import ToolOutcome
from services.llm.types import ToolDefinition


class Tool(ABC):
    name: str
    description: str
    args_schema: type[BaseModel]

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.args_schema.model_json_schema(),
        )

    @abstractmethod
    def run(self, args: BaseModel) -> ToolOutcome:
        """Execute with already-validated arguments. Must not raise for
        "no data found" conditions — that's a normal ToolOutcome(success=True,
        data={}, summary="no data available because ..."), not an exception.
        Exceptions are reserved for genuine execution failures (DB error,
        etc.), which the orchestrator catches and records as a failed
        ToolCallRecord rather than letting crash the whole request.
        """
