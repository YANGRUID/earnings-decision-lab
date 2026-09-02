"""Shared transport for any OpenAI-compatible /chat/completions endpoint.

DeepSeek, OpenAI itself, and arbitrary "OpenAI-compatible" servers all speak
the same wire format (confirmed against DeepSeek's current API docs, which
mirror OpenAI's chat completions schema field-for-field). This base class
implements that wire format exactly once; ``DeepSeekProvider``,
``OpenAIProvider``, and ``OpenAICompatibleProvider`` are still distinct,
separately-configured classes (see docs/llm_providers.md) so callers and
config always know which vendor they're actually talking to — this class is
an implementation detail, not something application code touches directly.

Structured output uses JSON mode (``response_format: {"type": "json_object"}``)
plus the target schema embedded in the prompt, not a stricter
schema-constrained decoding mode — this is the subset guaranteed to work
identically across DeepSeek and OpenAI (OpenAI's newer strict
``json_schema`` mode is not used, to keep behavior portable). See
docs/llm_providers.md for why this is an honest simplification, not a
silent gap.
"""

import json
import time
from collections.abc import Iterator

import httpx
from pydantic import ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from observability.http_client import new_http_client
from services.llm.base import LLMProvider, SchemaT
from services.llm.errors import LLMRequestError, StructuredOutputError
from services.llm.types import ChatMessage, GenerateResult, TokenUsage, ToolCall, ToolDefinition


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class _OpenAICompatibleTransport(LLMProvider):
    """Not registered as a usable provider on its own — subclasses set
    ``name``/``capabilities`` and are what the factory actually constructs.
    """

    def __init__(
        self, api_key: str, base_url: str, model: str, client: httpx.Client | None = None
    ) -> None:
        if not api_key:
            raise ValueError(f"{self.name} requires an API key")
        if not model:
            raise ValueError(f"{self.name} requires a model name (no hardcoded default)")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self.model = model  # public: callers that persist provenance need the configured model
        self._client = client or new_http_client(timeout=60.0)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _extra_payload_fields(self) -> dict:
        """Vendor-specific fields merged into every request. Empty by
        default; DeepSeekProvider uses this to send its explicit thinking
        configuration — see that class."""
        return {}

    def _sampling_fields(self, temperature: float) -> dict:
        """Sampling parameters for a request. A vendor mode that rejects
        them (DeepSeek thinking mode does not accept temperature/top_p)
        overrides this to omit them rather than send a field the API
        documents as unsupported."""
        return {"temperature": temperature}

    @staticmethod
    def _to_wire_messages(messages: list[ChatMessage]) -> list[dict]:
        wire = []
        for m in messages:
            entry: dict = {"role": m.role, "content": m.content}
            if m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
            if m.name:
                entry["name"] = m.name
            wire.append(entry)
        return wire

    @staticmethod
    def _to_wire_tools(tools: list[ToolDefinition] | None) -> list[dict] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    def _post(self, payload: dict) -> dict:
        response = self._client.post(
            f"{self._base_url}/chat/completions", headers=self._headers(), json=payload
        )
        if response.status_code >= 400:
            raise LLMRequestError(f"{self.name} returned {response.status_code}: {response.text}")
        return response.json()

    def _post_timed(self, payload: dict) -> tuple[dict, int]:
        """``_post`` plus wall-clock latency over all retry attempts."""
        start = time.monotonic()
        data = self._post(payload)
        return data, int((time.monotonic() - start) * 1000)

    def _parse_response(self, data: dict, latency_ms: int | None = None) -> GenerateResult:
        choice = data["choices"][0]
        message = choice["message"]
        reasoning = message.get("reasoning_content")
        tool_calls = [
            ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=json.loads(tc["function"]["arguments"] or "{}"),
            )
            for tc in message.get("tool_calls") or []
        ]
        usage = None
        if "usage" in data:
            u = data["usage"]
            details = u.get("completion_tokens_details") or {}
            usage = TokenUsage(
                input_tokens=u["prompt_tokens"],
                output_tokens=u["completion_tokens"],
                # Reported by DeepSeek; absent (None) on providers that
                # don't expose them -- never estimated.
                reasoning_tokens=details.get("reasoning_tokens"),
                cache_hit_tokens=u.get("prompt_cache_hit_tokens"),
                cache_miss_tokens=u.get("prompt_cache_miss_tokens"),
            )
        return GenerateResult(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            model=data.get("model"),
            latency_ms=latency_ms,
            # Presence and size only. The hidden reasoning text is dropped
            # here and never reaches persistence.
            reasoning_present=bool(reasoning),
            reasoning_chars=len(reasoning) if isinstance(reasoning, str) else None,
        )

    def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> GenerateResult:
        payload = {
            "model": self._model,
            "messages": self._to_wire_messages(messages),
            **self._sampling_fields(temperature),
            "max_tokens": max_tokens,
            **self._extra_payload_fields(),
        }
        wire_tools = self._to_wire_tools(tools)
        if wire_tools:
            payload["tools"] = wire_tools
        data, latency_ms = self._post_timed(payload)
        return self._parse_response(data, latency_ms)

    def generate_structured(
        self,
        messages: list[ChatMessage],
        schema: type[SchemaT],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> SchemaT:
        parsed, _ = self.generate_structured_result(
            messages, schema, temperature=temperature, max_tokens=max_tokens
        )
        return parsed

    def generate_structured_result(
        self,
        messages: list[ChatMessage],
        schema: type[SchemaT],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> tuple[SchemaT, GenerateResult]:
        """Structured generation returning the parsed object AND the
        response metadata (returned model, usage incl. reasoning/cache
        tokens, latency, finish reason) for provenance. A response that
        does not parse against ``schema`` raises StructuredOutputError --
        prose is never accepted, and no other model is tried."""
        schema_instruction = ChatMessage(
            role="system",
            content=(
                "Respond with a single JSON object matching this JSON Schema, "
                "and nothing else — no prose, no markdown fences:\n"
                f"{json.dumps(schema.model_json_schema())}"
            ),
        )
        payload = {
            "model": self._model,
            "messages": self._to_wire_messages([schema_instruction, *messages]),
            **self._sampling_fields(temperature),
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            **self._extra_payload_fields(),
        }
        data, latency_ms = self._post_timed(payload)
        result = self._parse_response(data, latency_ms)
        truncated = (
            f" (finish_reason={result.finish_reason}: the answer was cut off at max_tokens="
            f"{max_tokens}; in thinking mode hidden reasoning counts against this budget)"
            if result.finish_reason == "length"
            else ""
        )
        if not result.content:
            raise StructuredOutputError(
                f"{self.name} returned no content for structured request{truncated}"
            )
        try:
            parsed = schema.model_validate(json.loads(result.content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StructuredOutputError(
                f"{self.name} response did not match schema {schema.__name__}{truncated}: {exc}"
            ) from exc
        return parsed, result

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        payload = {
            "model": self._model,
            "messages": self._to_wire_messages(messages),
            **self._sampling_fields(temperature),
            "max_tokens": max_tokens,
            "stream": True,
            **self._extra_payload_fields(),
        }
        with self._client.stream(
            "POST", f"{self._base_url}/chat/completions", headers=self._headers(), json=payload
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise LLMRequestError(
                    f"{self.name} returned {response.status_code}: {response.text}"
                )
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line.removeprefix("data: ")
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                delta = chunk["choices"][0].get("delta", {})
                if "content" in delta and delta["content"]:
                    yield delta["content"]
