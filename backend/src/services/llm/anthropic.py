"""Anthropic adapter. Verified live against platform.claude.com/docs (2026-08):
Messages API at POST /v1/messages, x-api-key + anthropic-version headers,
system prompt as a top-level field (not a message with role="system"),
content as a list of typed blocks (text / tool_use), max_tokens required.

Structured output has no JSON-mode equivalent here, so it's implemented via
a *forced single tool call*: the target schema becomes a synthetic tool and
``tool_choice`` forces the model to call it — Anthropic's own documented
pattern for structured JSON output. This is a genuinely different mechanism
from the JSON-mode approach used by the OpenAI-compatible providers, not
just a different code path for the same idea — see docs/llm_providers.md.
"""

import json
from collections.abc import Iterator

import httpx
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from services.llm.base import LLMProvider
from services.llm.errors import LLMRequestError, StructuredOutputError
from services.llm.types import (
    Capabilities,
    ChatMessage,
    GenerateResult,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
_STRUCTURED_TOOL_NAME = "emit_result"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    capabilities = Capabilities(
        supports_structured_output=True,  # via forced tool call, not JSON mode
        supports_tool_calling=True,
        supports_streaming=True,
    )

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("anthropic requires an API key")
        if not model:
            raise ValueError("anthropic requires a model name (no hardcoded default)")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.Client(timeout=60.0)

    def _headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _split_system(messages: list[ChatMessage]) -> tuple[str | None, list[ChatMessage]]:
        system_parts = [m.content for m in messages if m.role == "system"]
        rest = [m for m in messages if m.role != "system"]
        return ("\n\n".join(system_parts) if system_parts else None), rest

    @staticmethod
    def _to_wire_messages(messages: list[ChatMessage]) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in messages]

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    def _post(self, payload: dict) -> dict:
        response = self._client.post(
            f"{self._base_url}/v1/messages", headers=self._headers(), json=payload
        )
        if response.status_code >= 400:
            raise LLMRequestError(f"anthropic returned {response.status_code}: {response.text}")
        return response.json()

    def _parse_response(self, data: dict) -> GenerateResult:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(
                    ToolCall(id=block["id"], name=block["name"], arguments=block["input"])
                )
        usage = None
        if "usage" in data:
            usage = TokenUsage(
                input_tokens=data["usage"]["input_tokens"],
                output_tokens=data["usage"]["output_tokens"],
            )
        return GenerateResult(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason"),
            usage=usage,
            model=data.get("model"),
        )

    def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> GenerateResult:
        system, rest = self._split_system(messages)
        payload: dict = {
            "model": self._model,
            "messages": self._to_wire_messages(rest),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        return self._parse_response(self._post(payload))

    def generate_structured(
        self,
        messages: list[ChatMessage],
        schema: type[BaseModel],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> BaseModel:
        system, rest = self._split_system(messages)
        payload: dict = {
            "model": self._model,
            "messages": self._to_wire_messages(rest),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": [
                {
                    "name": _STRUCTURED_TOOL_NAME,
                    "description": f"Emit the result as {schema.__name__}.",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": _STRUCTURED_TOOL_NAME},
        }
        if system:
            payload["system"] = system
        result = self._parse_response(self._post(payload))
        matching = [tc for tc in result.tool_calls if tc.name == _STRUCTURED_TOOL_NAME]
        if not matching:
            raise StructuredOutputError("anthropic did not return the forced structured tool call")
        try:
            return schema.model_validate(matching[0].arguments)
        except ValidationError as exc:
            raise StructuredOutputError(
                f"anthropic tool call did not match schema {schema.__name__}: {exc}"
            ) from exc

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        system, rest = self._split_system(messages)
        payload: dict = {
            "model": self._model,
            "messages": self._to_wire_messages(rest),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if system:
            payload["system"] = system
        with self._client.stream(
            "POST", f"{self._base_url}/v1/messages", headers=self._headers(), json=payload
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise LLMRequestError(
                    f"anthropic returned {response.status_code}: {response.text}"
                )
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line.removeprefix("data: "))
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta["text"]
