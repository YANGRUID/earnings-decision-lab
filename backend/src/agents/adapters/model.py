"""LangChain-compatible chat model over this project's own LLMProvider.

Direction of the dependency, deliberately (requirement 7/8): LangChain is
the *interface* this project now speaks, and ``services.llm.base.LLMProvider``
remains the *implementation* underneath it. The adapter is a facade, not a
replacement.

That is not framework conservatism. A native ``langchain-deepseek`` model
would silently drop behaviour this project already depends on:

- ``thinking``/``reasoning_effort`` as an explicit, fail-closed request
  (``LLMConfigurationError`` before any HTTP call, never a silent
  downgrade to the API's default reasoning mode -- see
  services/llm/factory.py and services/v4_decision_view_config.py);
- ``reasoning_tokens``, ``prompt_cache_hit_tokens`` and
  ``prompt_cache_miss_tokens``, which DeepSeek reports and this project
  bills and reports on (services/usage_instrumentation.py);
- ``reasoning_present``/``reasoning_chars`` -- the *presence* and *size*
  of hidden reasoning recorded without ever persisting the reasoning text
  (docs/llm_providers.md, and requirement 42);
- the real per-provider structured-output normalisation (JSON mode plus
  prompt for one provider, a forced tool call for another), which is why
  ``with_structured_output`` below delegates rather than reimplements;
- the error taxonomy in services/llm/errors.py, which the API layer maps
  to real HTTP states.

So: provider behaviour stays ours, and the graph still gets a true
``BaseChatModel`` it can bind tools to and compose into a Runnable.
"""

from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict

from services.llm.base import LLMProvider
from services.llm.errors import LLMConfigurationError
from services.llm.types import ChatMessage, GenerateResult, ToolDefinition


def _to_provider_messages(messages: Sequence[BaseMessage]) -> list[ChatMessage]:
    """LangChain message objects -> this project's ChatMessage.

    An unmapped message type raises rather than being coerced to "user":
    a ``FunctionMessage`` quietly relabelled as a question is the kind of
    silent reinterpretation that makes a trace untrustworthy.
    """
    out: list[ChatMessage] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            out.append(ChatMessage(role="system", content=_text(message)))
        elif isinstance(message, HumanMessage):
            out.append(ChatMessage(role="user", content=_text(message)))
        elif isinstance(message, ToolMessage):
            out.append(
                ChatMessage(
                    role="tool",
                    content=_text(message),
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                )
            )
        elif isinstance(message, AIMessage):
            out.append(ChatMessage(role="assistant", content=_text(message)))
        else:
            raise LLMConfigurationError(
                f"cannot send a {type(message).__name__} through the EDL provider "
                f"abstraction -- it has no honest ChatMessage role"
            )
    return out


def _text(message: BaseMessage) -> str:
    """``content`` is ``str | list`` in LangChain. Multi-part content is
    flattened to its text parts only; a non-text part (an image block) is
    not silently dropped, because this project's providers are configured
    for text and pretending otherwise would lose evidence."""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            raise LLMConfigurationError(
                "multi-modal message content is not supported by the configured "
                "EDL LLM provider"
            )
    return "\n".join(parts)


def _to_tool_definitions(
    tools: Sequence[dict[str, Any] | type | Any | BaseTool],
) -> list[ToolDefinition]:
    """Any LangChain tool form -> this project's ToolDefinition, via
    LangChain's own OpenAI-shaped normaliser so a plain callable, a
    pydantic class and a BaseTool all land in the same place."""
    definitions: list[ToolDefinition] = []
    for tool in tools:
        spec = convert_to_openai_tool(tool)["function"]
        definitions.append(
            ToolDefinition(
                name=spec["name"],
                description=spec.get("description", ""),
                parameters=spec.get("parameters", {"type": "object", "properties": {}}),
            )
        )
    return definitions


def _response_metadata(result: GenerateResult, fallback_model: str) -> dict[str, Any]:
    """Operational provenance only. ``reasoning_present``/``reasoning_chars``
    describe hidden reasoning without carrying it (requirement 42)."""
    meta: dict[str, Any] = {
        "model": result.model or fallback_model,
        "finish_reason": result.finish_reason,
        "reasoning_present": result.reasoning_present,
    }
    if result.latency_ms is not None:
        meta["latency_ms"] = result.latency_ms
    if result.reasoning_chars is not None:
        meta["reasoning_chars"] = result.reasoning_chars
    if result.usage is not None:
        if result.usage.reasoning_tokens is not None:
            meta["reasoning_tokens"] = result.usage.reasoning_tokens
        if result.usage.cache_hit_tokens is not None:
            meta["cache_hit_tokens"] = result.usage.cache_hit_tokens
        if result.usage.cache_miss_tokens is not None:
            meta["cache_miss_tokens"] = result.usage.cache_miss_tokens
    return meta


def _usage_metadata(result: GenerateResult) -> UsageMetadata | None:
    if result.usage is None:
        return None
    return UsageMetadata(
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        total_tokens=result.usage.input_tokens + result.usage.output_tokens,
    )


class EDLChatModel(BaseChatModel):
    """``BaseChatModel`` whose transport is an ``LLMProvider``.

    Provider errors (``services.llm.errors``) are deliberately NOT wrapped:
    a ``MissingAPIKeyError`` must still arrive at the API layer as itself
    (requirement 39).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: LLMProvider
    temperature: float = 0.0
    max_tokens: int = 1024

    @property
    def _llm_type(self) -> str:
        return f"edl-{self.provider.name}"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        """What identifies this model for LangChain's own caching/tracing.
        No API key, no base URL, no header -- requirement 90."""
        return {
            "provider": self.provider.name,
            "model": self.provider.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if stop:
            # The EDL provider abstraction has no stop-sequence field, so
            # honouring this would mean inventing one per provider. Raising
            # is the honest answer; silently ignoring it would make the
            # model appear to respect a constraint it never received.
            raise LLMConfigurationError(
                "stop sequences are not supported by the EDL LLM provider abstraction"
            )
        tools = kwargs.get("tools")
        result = self.provider.generate(
            _to_provider_messages(messages),
            tools=_to_tool_definitions(tools) if tools else None,
            temperature=kwargs.get("temperature", self.temperature),
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
        )
        message = AIMessage(
            content=result.content or "",
            tool_calls=[
                {"name": tc.name, "args": tc.arguments, "id": tc.id, "type": "tool_call"}
                for tc in result.tool_calls
            ],
            usage_metadata=_usage_metadata(result),
            response_metadata=_response_metadata(result, self.provider.model),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        if not self.provider.capabilities.supports_streaming:
            raise LLMConfigurationError(
                f"provider {self.provider.name!r} does not support streaming"
            )
        if stop:
            raise LLMConfigurationError(
                "stop sequences are not supported by the EDL LLM provider abstraction"
            )
        for chunk in self.provider.stream(
            _to_provider_messages(messages),
            temperature=kwargs.get("temperature", self.temperature),
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
        ):
            yield ChatGenerationChunk(message=AIMessageChunk(content=chunk))

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        """Fails closed on a provider without tool calling rather than
        returning a model that quietly ignores the tools it was given --
        the orchestrator's existing capability branch
        (``supports_tool_calling``) exists precisely because providers
        differ here, and the graph keeps that branch."""
        if not self.provider.capabilities.supports_tool_calling:
            raise LLMConfigurationError(
                f"provider {self.provider.name!r} does not support native tool calling; "
                f"use the structured-planner path instead"
            )
        if tool_choice is not None:
            raise LLMConfigurationError(
                "tool_choice is not expressible through the EDL LLM provider abstraction"
            )
        return self.bind(tools=list(tools), **kwargs)

    def with_structured_output(
        self,
        schema: dict | type,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        """Delegates to ``LLMProvider.generate_structured`` instead of
        LangChain's default function-calling/JSON-mode strategy.

        This is the whole point of requirement 11: there is ONE canonical
        structured-output path per provider in this codebase and it already
        knows which providers need JSON mode plus a prompt and which need a
        forced tool call. Re-deriving that here would create a second,
        divergent answer to the same question -- and a
        ``StructuredOutputError`` raised from our path is the failure the
        API layer already understands.
        """
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise LLMConfigurationError(
                "structured output requires a pydantic BaseModel subclass -- this "
                "project's canonical schemas (schemas/agent.py, schemas/decision.py) "
                "are the one definition per concept"
            )
        if include_raw:
            raise LLMConfigurationError(
                "include_raw is not supported: the EDL structured path returns the "
                "validated schema, and its provenance is recorded separately"
            )
        provider = self.provider
        temperature = kwargs.get("temperature", self.temperature)
        max_tokens = kwargs.get("max_tokens", self.max_tokens)

        def _invoke(messages: Any) -> BaseModel:
            return provider.generate_structured(
                _to_provider_messages(_coerce_messages(messages)),
                schema,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        return RunnableLambda(_invoke, name=f"edl_structured_{schema.__name__}")


def _coerce_messages(value: Any) -> list[BaseMessage]:
    """Accepts what LangChain's ``LanguageModelInput`` accepts: a string, a
    single message, or a sequence of messages."""
    if isinstance(value, str):
        return [HumanMessage(content=value)]
    if isinstance(value, BaseMessage):
        return [value]
    if hasattr(value, "to_messages"):
        return list(value.to_messages())
    return list(value)
