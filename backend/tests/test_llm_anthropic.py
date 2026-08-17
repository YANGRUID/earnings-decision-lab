import json

import httpx
import pytest
from pydantic import BaseModel

from services.llm.anthropic import AnthropicProvider
from services.llm.errors import LLMRequestError, StructuredOutputError
from services.llm.types import ChatMessage, ToolDefinition

BASE_URL = "https://api.anthropic.com"


def _provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key", model="claude-test", base_url=BASE_URL)


class Weather(BaseModel):
    city: str
    sunny: bool


def test_generate_parses_text_content(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/messages",
        json={
            "model": "claude-test",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    )
    result = _provider().generate([ChatMessage(role="user", content="hi")])

    assert result.content == "hello"
    assert result.finish_reason == "end_turn"
    assert result.usage.input_tokens == 5


def test_generate_splits_system_message_to_top_level_field(httpx_mock):
    def check_request(request):
        body = json.loads(request.content)
        assert body["system"] == "be terse"
        assert all(m["role"] != "system" for m in body["messages"])
        return httpx.Response(
            200,
            json={
                "model": "claude-test",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            },
        )

    httpx_mock.add_callback(check_request, url=f"{BASE_URL}/v1/messages")

    _provider().generate(
        [ChatMessage(role="system", content="be terse"), ChatMessage(role="user", content="hi")]
    )


def test_generate_parses_tool_use_block(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/messages",
        json={
            "model": "claude-test",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {"city": "Zurich"},
                }
            ],
            "stop_reason": "tool_use",
        },
    )
    tools = [ToolDefinition(name="get_weather", description="d", parameters={"type": "object"})]

    result = _provider().generate([ChatMessage(role="user", content="weather?")], tools=tools)

    assert result.tool_calls[0].name == "get_weather"
    assert result.tool_calls[0].arguments == {"city": "Zurich"}


def test_generate_structured_uses_forced_tool_call(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/messages",
        json={
            "model": "claude-test",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "emit_result",
                    "input": {"city": "Zurich", "sunny": True},
                }
            ],
            "stop_reason": "tool_use",
        },
    )
    result = _provider().generate_structured(
        [ChatMessage(role="user", content="weather?")], Weather
    )

    assert isinstance(result, Weather)
    assert result.city == "Zurich"


def test_generate_structured_raises_when_tool_not_called(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/messages",
        json={
            "model": "claude-test",
            "content": [{"type": "text", "text": "I refuse"}],
            "stop_reason": "end_turn",
        },
    )
    with pytest.raises(StructuredOutputError):
        _provider().generate_structured([ChatMessage(role="user", content="weather?")], Weather)


def test_generate_raises_llm_request_error_on_4xx(httpx_mock):
    httpx_mock.add_response(url=f"{BASE_URL}/v1/messages", status_code=401, text="unauthorized")

    with pytest.raises(LLMRequestError):
        _provider().generate([ChatMessage(role="user", content="hi")])


def test_stream_yields_text_deltas(httpx_mock):
    sse_body = (
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/messages",
        headers={"Content-Type": "text/event-stream"},
        text=sse_body,
    )

    chunks = list(_provider().stream([ChatMessage(role="user", content="hi")]))

    assert "".join(chunks) == "Hello"


def test_requires_api_key():
    with pytest.raises(ValueError):
        AnthropicProvider(api_key="", model="claude-test")


def test_requires_model_name():
    with pytest.raises(ValueError):
        AnthropicProvider(api_key="key", model="")
