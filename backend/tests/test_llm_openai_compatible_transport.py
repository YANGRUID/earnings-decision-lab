"""Tests the shared OpenAI-compatible transport once, exercised through
both DeepSeekProvider and OpenAIProvider — since they're the same wire
format, duplicating these tests per-subclass would just be noise.
"""

import pytest
from pydantic import BaseModel

from services.llm.deepseek import DeepSeekProvider
from services.llm.errors import LLMRequestError, StructuredOutputError
from services.llm.openai import OpenAIProvider
from services.llm.types import ChatMessage, ToolDefinition

DEEPSEEK = ("deepseek", DeepSeekProvider, "https://api.deepseek.com")
OPENAI = ("openai", OpenAIProvider, "https://api.openai.com/v1")


def _make(provider_cls, base_url):
    return provider_cls(api_key="test-key", model="test-model", base_url=base_url)


class Weather(BaseModel):
    city: str
    sunny: bool


@pytest.mark.parametrize(("name", "cls", "base_url"), [DEEPSEEK, OPENAI])
def test_generate_parses_content(httpx_mock, name, cls, base_url):
    httpx_mock.add_response(
        url=f"{base_url}/chat/completions",
        json={
            "model": "test-model",
            "choices": [
                {"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    )
    provider = _make(cls, base_url)

    result = provider.generate([ChatMessage(role="user", content="hi")])

    assert result.content == "hello"
    assert result.finish_reason == "stop"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 2


@pytest.mark.parametrize(("name", "cls", "base_url"), [DEEPSEEK, OPENAI])
def test_generate_parses_tool_calls(httpx_mock, name, cls, base_url):
    httpx_mock.add_response(
        url=f"{base_url}/chat/completions",
        json={
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "Zurich"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
    )
    provider = _make(cls, base_url)
    tools = [ToolDefinition(name="get_weather", description="d", parameters={"type": "object"})]

    result = provider.generate([ChatMessage(role="user", content="weather?")], tools=tools)

    assert result.tool_calls[0].name == "get_weather"
    assert result.tool_calls[0].arguments == {"city": "Zurich"}


@pytest.mark.parametrize(("name", "cls", "base_url"), [DEEPSEEK, OPENAI])
def test_generate_structured_validates_schema(httpx_mock, name, cls, base_url):
    httpx_mock.add_response(
        url=f"{base_url}/chat/completions",
        json={
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"city": "Zurich", "sunny": true}',
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )
    provider = _make(cls, base_url)

    result = provider.generate_structured([ChatMessage(role="user", content="weather?")], Weather)

    assert isinstance(result, Weather)
    assert result.city == "Zurich"
    assert result.sunny is True


@pytest.mark.parametrize(("name", "cls", "base_url"), [DEEPSEEK, OPENAI])
def test_generate_structured_raises_on_invalid_json(httpx_mock, name, cls, base_url):
    httpx_mock.add_response(
        url=f"{base_url}/chat/completions",
        json={
            "model": "test-model",
            "choices": [
                {"message": {"role": "assistant", "content": "not json"}, "finish_reason": "stop"}
            ],
        },
    )
    provider = _make(cls, base_url)

    with pytest.raises(StructuredOutputError):
        provider.generate_structured([ChatMessage(role="user", content="weather?")], Weather)


@pytest.mark.parametrize(("name", "cls", "base_url"), [DEEPSEEK, OPENAI])
def test_generate_structured_raises_when_schema_mismatch(httpx_mock, name, cls, base_url):
    httpx_mock.add_response(
        url=f"{base_url}/chat/completions",
        json={
            "model": "test-model",
            "choices": [
                {
                    "message": {"role": "assistant", "content": '{"wrong_field": 1}'},
                    "finish_reason": "stop",
                }
            ],
        },
    )
    provider = _make(cls, base_url)

    with pytest.raises(StructuredOutputError):
        provider.generate_structured([ChatMessage(role="user", content="weather?")], Weather)


@pytest.mark.parametrize(("name", "cls", "base_url"), [DEEPSEEK, OPENAI])
def test_generate_raises_llm_request_error_on_4xx(httpx_mock, name, cls, base_url):
    httpx_mock.add_response(
        url=f"{base_url}/chat/completions", status_code=401, text="unauthorized"
    )
    provider = _make(cls, base_url)

    with pytest.raises(LLMRequestError):
        provider.generate([ChatMessage(role="user", content="hi")])


@pytest.mark.parametrize(("name", "cls", "base_url"), [DEEPSEEK, OPENAI])
def test_stream_yields_text_chunks(httpx_mock, name, cls, base_url):
    sse_body = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    httpx_mock.add_response(
        url=f"{base_url}/chat/completions",
        headers={"Content-Type": "text/event-stream"},
        text=sse_body,
    )
    provider = _make(cls, base_url)

    chunks = list(provider.stream([ChatMessage(role="user", content="hi")]))

    assert "".join(chunks) == "Hello"


def test_requires_api_key():
    with pytest.raises(ValueError):
        DeepSeekProvider(api_key="", model="deepseek-v4-flash")


def test_requires_model_name_not_hardcoded():
    with pytest.raises(ValueError):
        DeepSeekProvider(api_key="key", model="")
