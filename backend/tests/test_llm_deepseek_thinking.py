"""DeepSeek thinking mode (2026-09-02): the exact documented request shape,
what is parsed from the response, and what is never accepted or stored.

Verified contract (api-docs.deepseek.com/api/create-chat-completion):
request ``thinking: {"type": "enabled"|"disabled", "reasoning_effort":
"low"|"high"|"max"}``; response ``choices[].message.reasoning_content``
beside ``content``; usage ``completion_tokens_details.reasoning_tokens``,
``prompt_cache_hit_tokens``; thinking mode does not accept ``temperature``.
Every request here goes through an httpx MockTransport so the real wire
payload is asserted -- no network, no real key.
"""

import json

import httpx
import pytest
from pydantic import BaseModel

from services.llm.deepseek import DeepSeekProvider
from services.llm.errors import StructuredOutputError
from services.llm.types import ChatMessage


class Probe(BaseModel):
    direction: str
    rationale: str


def _provider(*, thinking="disabled", effort=None, handler=None, model="deepseek-v4-pro"):
    p = DeepSeekProvider(
        api_key="test-key", model=model, thinking=thinking, reasoning_effort=effort
    )
    if handler is not None:
        p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def _capture(response_json: dict, status: int = 200):
    """A handler that records every request body and returns a canned reply."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(status, json=response_json)

    return handler, seen


def _reply(content: str, *, reasoning: str | None = None, model="deepseek-v4-pro",
           finish="stop", reasoning_tokens=None, cache_hit=None):
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    usage: dict = {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}
    if cache_hit is not None:
        usage["prompt_cache_hit_tokens"] = cache_hit
        usage["prompt_cache_miss_tokens"] = 120 - cache_hit
    if reasoning_tokens is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return {
        "id": "x",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    }


class TestRequestConstruction:
    def test_thinking_enabled_sends_the_documented_field_and_omits_temperature(self):
        handler, seen = _capture(_reply('{"direction": "neutral", "rationale": "r"}'))
        p = _provider(thinking="enabled", effort="high", handler=handler)
        p.generate_structured([ChatMessage(role="user", content="q")], Probe, max_tokens=8000)
        body = seen[0]
        assert body["model"] == "deepseek-v4-pro"
        assert body["thinking"] == {"type": "enabled", "reasoning_effort": "high"}
        assert "temperature" not in body  # documented as unsupported in thinking mode
        assert body["max_tokens"] == 8000
        assert body["response_format"] == {"type": "json_object"}
        assert "reasoning_effort" not in body  # nested under thinking, never top-level

    def test_thinking_disabled_is_sent_explicitly_with_temperature(self):
        handler, seen = _capture(_reply("OK"))
        p = _provider(handler=handler, model="deepseek-v4-flash")
        p.generate([ChatMessage(role="user", content="q")], max_tokens=5)
        body = seen[0]
        assert body["thinking"] == {"type": "disabled"}
        assert body["temperature"] == 0.0
        assert body["model"] == "deepseek-v4-flash"

    @pytest.mark.parametrize("effort", ["low", "high", "max"])
    def test_every_documented_effort_is_accepted(self, effort):
        p = _provider(thinking="enabled", effort=effort)
        assert p._extra_payload_fields()["thinking"]["reasoning_effort"] == effort

    def test_unsupported_effort_and_bad_combinations_fail_before_any_request(self):
        with pytest.raises(ValueError, match="reasoning_effort"):
            _provider(thinking="enabled", effort="ultra")
        with pytest.raises(ValueError, match="requires thinking"):
            _provider(thinking="disabled", effort="high")
        with pytest.raises(ValueError, match="thinking"):
            DeepSeekProvider(api_key="k", model="m", thinking="on")  # type: ignore[arg-type]

    def test_provider_exposes_its_configuration_for_provenance(self):
        p = _provider(thinking="enabled", effort="max")
        assert (p.model, p.thinking, p.reasoning_effort) == ("deepseek-v4-pro", "enabled", "max")


class TestResponseParsing:
    def test_returned_model_usage_reasoning_and_latency_are_captured(self):
        handler, _ = _capture(
            _reply(
                '{"direction": "bullish", "rationale": "evidence"}',
                reasoning="hidden chain of thought " * 20,
                model="deepseek-v4-pro-2026-08",
                reasoning_tokens=777,
                cache_hit=64,
            )
        )
        p = _provider(thinking="enabled", effort="high", handler=handler)
        parsed, meta = p.generate_structured_result(
            [ChatMessage(role="user", content="q")], Probe, max_tokens=8000
        )
        assert parsed.direction == "bullish"
        assert meta.model == "deepseek-v4-pro-2026-08"  # stored as returned, not the alias
        assert meta.finish_reason == "stop"
        assert meta.usage is not None
        assert (meta.usage.input_tokens, meta.usage.output_tokens) == (120, 40)
        assert meta.usage.reasoning_tokens == 777
        assert meta.usage.cache_hit_tokens == 64
        assert meta.usage.cache_miss_tokens == 56
        assert meta.latency_ms is not None and meta.latency_ms >= 0
        # Presence and size only -- the reasoning text itself is not carried.
        assert meta.reasoning_present is True
        assert meta.reasoning_chars == len("hidden chain of thought " * 20)
        assert not hasattr(meta, "reasoning_content")
        assert "hidden chain" not in meta.model_dump_json()

    def test_absent_optional_usage_fields_stay_none_never_estimated(self):
        handler, _ = _capture(_reply('{"direction": "neutral", "rationale": "r"}'))
        p = _provider(thinking="enabled", effort="high", handler=handler)
        _, meta = p.generate_structured_result([ChatMessage(role="user", content="q")], Probe)
        assert meta.usage is not None
        assert meta.usage.reasoning_tokens is None
        assert meta.usage.cache_hit_tokens is None
        assert meta.reasoning_present is False


class TestFailureIsHonest:
    def test_malformed_structured_output_raises_and_no_other_model_is_tried(self):
        handler, seen = _capture(_reply("Here is my view in prose, not JSON."))
        p = _provider(thinking="enabled", effort="high", handler=handler)
        with pytest.raises(StructuredOutputError, match="did not match schema"):
            p.generate_structured([ChatMessage(role="user", content="q")], Probe)
        assert len(seen) == 1
        assert all(b["model"] == "deepseek-v4-pro" for b in seen)  # never a flash retry

    def test_truncation_by_the_token_budget_is_named_in_the_error(self):
        handler, _ = _capture(_reply("", reasoning="thinking...", finish="length"))
        p = _provider(thinking="enabled", effort="high", handler=handler)
        with pytest.raises(StructuredOutputError, match="finish_reason=length"):
            p.generate_structured([ChatMessage(role="user", content="q")], Probe, max_tokens=4096)

    def test_provider_error_surfaces_as_a_request_error(self):
        from services.llm.errors import LLMRequestError

        handler, seen = _capture({"error": {"message": "invalid model"}}, status=400)
        p = _provider(thinking="enabled", effort="high", handler=handler)
        with pytest.raises(LLMRequestError, match="400"):
            p.generate_structured([ChatMessage(role="user", content="q")], Probe)
        assert len(seen) == 1  # 4xx is not retried and never re-sent with another model
