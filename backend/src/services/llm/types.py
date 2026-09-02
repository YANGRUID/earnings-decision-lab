"""Provider-agnostic LLM types. Application code (RAG, extraction, agents)
depends only on these — never on an OpenAI/Anthropic/DeepSeek SDK type.
"""

from typing import Literal

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None  # set on role="tool" replies
    name: str | None = None  # tool name, set on role="tool" replies


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict  # JSON Schema object


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    # Only ever set from what the provider's own response reports -- never
    # estimated. DeepSeek: completion_tokens_details.reasoning_tokens,
    # prompt_cache_hit_tokens, prompt_cache_miss_tokens.
    reasoning_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None


class GenerateResult(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = []
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    #: The model identity the API itself reported for this response (may be
    #: an alias, may be a dated version -- stored as returned, never invented).
    model: str | None = None
    #: Wall-clock latency of the HTTP round trip(s), measured by the transport.
    latency_ms: int | None = None
    #: Whether the response carried hidden reasoning (DeepSeek
    #: ``reasoning_content``). Only its presence and size are recorded -- the
    #: reasoning text itself is never persisted (see docs/llm_providers.md).
    reasoning_present: bool = False
    reasoning_chars: int | None = None


class Capabilities(BaseModel):
    """What a provider actually supports, so callers (especially the agent
    layer) can branch on real capability rather than assuming every model
    supports every feature identically. See docs/llm_providers.md for how
    each provider's capabilities differ in practice, not just in principle.
    """

    supports_structured_output: bool
    supports_tool_calling: bool
    supports_streaming: bool
