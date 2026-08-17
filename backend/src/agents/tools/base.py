"""Tool interface. Every agent tool wraps an already-real, already-tested
piece of this project (a DB query, the RAG pipeline, or the deterministic
options engine) — the agent layer adds orchestration on top of existing
functionality, it does not reimplement it.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel

from agents.tools.types import ToolOutcome
from services.llm.types import ToolDefinition


class Tool[ArgsT: BaseModel](ABC):
    """Generic over its own args schema so each concrete tool's ``run``
    can take its specific args type without violating Liskov substitution
    from mypy's perspective — the registry/orchestrator (agents/orchestrator.py)
    still handles tools polymorphically via ``Tool[Any]``, since which
    concrete args type applies is only known at the JSON-validation
    boundary (``args_schema.model_validate(arguments)``), not statically.
    Uses PEP 695 native generic syntax (Python 3.12+, this project's
    minimum version) rather than the older ``Generic[ArgsT]`` + module-level
    ``TypeVar`` pattern.
    """

    name: str
    description: str
    args_schema: type[ArgsT]

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.args_schema.model_json_schema(),
        )

    @abstractmethod
    def run(self, args: ArgsT) -> ToolOutcome:
        """Execute with already-validated arguments. Must not raise for
        "no data found" conditions — that's a normal ToolOutcome(success=True,
        data={}, summary="no data available because ..."), not an exception.
        Exceptions are reserved for genuine execution failures (DB error,
        etc.), which the orchestrator catches and records as a failed
        ToolCallRecord rather than letting crash the whole request.
        """
