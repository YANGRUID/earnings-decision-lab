"""The provider-agnostic interface. RAG, extraction, and agent code depend
on this ABC — never on a provider SDK directly — so swapping DeepSeek for
Anthropic (or adding a new provider) never touches a caller.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TypeVar

from pydantic import BaseModel

from services.llm.types import Capabilities, ChatMessage, GenerateResult, ToolDefinition

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMProvider(ABC):
    name: str
    model: str
    capabilities: Capabilities

    @abstractmethod
    def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> GenerateResult:
        """One non-streaming completion, optionally with tool definitions."""

    @abstractmethod
    def generate_structured(
        self,
        messages: list[ChatMessage],
        schema: type[SchemaT],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> SchemaT:
        """A completion validated against ``schema``. Providers normalize
        this differently (JSON mode + prompt vs. forced tool call) — see
        docs/llm_providers.md for exactly what differs per provider. Raises
        ``services.llm.errors.StructuredOutputError`` if the response can't
        be validated against ``schema``.
        """

    def generate_structured_result(
        self,
        messages: list[ChatMessage],
        schema: type[SchemaT],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> tuple[SchemaT, GenerateResult]:
        """Structured generation that also returns the response metadata a
        caller needs to persist provenance (returned model, usage, latency,
        finish reason). The default just wraps ``generate_structured`` with
        the configured model; transports that see the raw response override
        it with the real metadata."""
        parsed = self.generate_structured(
            messages, schema, temperature=temperature, max_tokens=max_tokens
        )
        return parsed, GenerateResult(model=self.model)

    @abstractmethod
    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        """Yields text chunks. Callers must check
        ``capabilities.supports_streaming`` first — a provider without
        streaming support raises rather than silently returning the whole
        response as one chunk pretending to be a stream.
        """
