"""The provider-agnostic interface. RAG, extraction, and agent code depend
on this ABC — never on a provider SDK directly — so swapping DeepSeek for
Anthropic (or adding a new provider) never touches a caller.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator

from pydantic import BaseModel

from services.llm.types import Capabilities, ChatMessage, GenerateResult, ToolDefinition


class LLMProvider(ABC):
    name: str
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
        schema: type[BaseModel],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> BaseModel:
        """A completion validated against ``schema``. Providers normalize
        this differently (JSON mode + prompt vs. forced tool call) — see
        docs/llm_providers.md for exactly what differs per provider. Raises
        ``services.llm.errors.StructuredOutputError`` if the response can't
        be validated against ``schema``.
        """

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
