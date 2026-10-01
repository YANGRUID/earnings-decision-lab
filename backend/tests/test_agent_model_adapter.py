"""The LangChain chat-model adapter (requirement 66).

What matters here is not that the adapter compiles, but that it refuses
where it said it would and carries provenance where it said it would. An
adapter that silently drops a constraint is the specific failure mode
these tests exist to catch.
"""

import pytest
from _agent_fakes import _ScriptedLLM
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agents.adapters.model import EDLChatModel, _to_provider_messages
from schemas.agent import IntentCategory, IntentClassification
from services.llm.errors import LLMConfigurationError, LLMRequestError, StructuredOutputError
from services.llm.types import GenerateResult, TokenUsage, ToolCall


def _model(**kwargs) -> EDLChatModel:
    return EDLChatModel(provider=_ScriptedLLM(**kwargs))


def test_llm_type_names_the_underlying_provider():
    assert _model()._llm_type == "edl-scripted"


def test_identifying_params_carry_no_credentials():
    params = _model()._identifying_params
    assert params == {
        "provider": "scripted",
        "model": "deepseek-v4-flash",
        "temperature": 0.0,
        "max_tokens": 1024,
    }


def test_every_message_role_maps_without_relabelling():
    mapped = _to_provider_messages(
        [
            SystemMessage(content="sys"),
            HumanMessage(content="ask"),
            AIMessage(content="draft"),
            ToolMessage(content="result", tool_call_id="c1", name="search_filings"),
        ]
    )
    assert [m.role for m in mapped] == ["system", "user", "assistant", "tool"]
    assert mapped[3].tool_call_id == "c1"
    assert mapped[3].name == "search_filings"


def test_an_unmappable_message_raises_rather_than_becoming_a_user_turn():
    class _Odd(HumanMessage):
        pass

    # A subclass still maps (isinstance holds); the guard is for a message
    # type with no honest role at all.
    from langchain_core.messages import FunctionMessage

    with pytest.raises(LLMConfigurationError, match="no honest ChatMessage role"):
        _to_provider_messages([FunctionMessage(content="x", name="f")])
    assert _to_provider_messages([_Odd(content="still a question")])[0].role == "user"


def test_generate_carries_usage_and_provider_reported_model():
    model = _model(
        generate_responses=[
            GenerateResult(
                content="answer",
                usage=TokenUsage(
                    input_tokens=11, output_tokens=7, reasoning_tokens=4, cache_hit_tokens=3
                ),
                model="deepseek-v4-flash-2026-09-01",
                latency_ms=120,
                reasoning_present=True,
                reasoning_chars=840,
            )
        ]
    )
    message = model.invoke([HumanMessage(content="q")])
    assert message.content == "answer"
    assert message.usage_metadata == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    meta = message.response_metadata
    # The model the API actually reported, not the one we asked for.
    assert meta["model"] == "deepseek-v4-flash-2026-09-01"
    assert meta["reasoning_tokens"] == 4
    assert meta["cache_hit_tokens"] == 3
    assert meta["latency_ms"] == 120


def test_hidden_reasoning_is_described_but_never_carried():
    model = _model(
        generate_responses=[
            GenerateResult(content="a", reasoning_present=True, reasoning_chars=840)
        ]
    )
    meta = model.invoke([HumanMessage(content="q")]).response_metadata
    assert meta["reasoning_present"] is True
    assert meta["reasoning_chars"] == 840
    # Requirement 42: the size, never the text.
    assert not any(
        isinstance(v, str) and len(v) > 100 for v in meta.values()
    ), "no long free text belongs in response metadata"


def test_tool_calls_survive_the_adapter():
    model = _model(
        generate_responses=[
            GenerateResult(
                tool_calls=[
                    ToolCall(id="c1", name="get_historical_earnings", arguments={"ticker": "MU"})
                ]
            )
        ]
    )
    message = model.invoke([HumanMessage(content="q")])
    assert message.tool_calls == [
        {
            "name": "get_historical_earnings",
            "args": {"ticker": "MU"},
            "id": "c1",
            "type": "tool_call",
        }
    ]


def test_stop_sequences_raise_instead_of_being_ignored():
    model = _model(generate_responses=[GenerateResult(content="a")])
    with pytest.raises(LLMConfigurationError, match="stop sequences"):
        model.invoke([HumanMessage(content="q")], stop=["\n\n"])


def test_bind_tools_refuses_a_provider_without_tool_calling():
    model = _model(supports_tool_calling=False)
    with pytest.raises(LLMConfigurationError, match="does not support native tool calling"):
        model.bind_tools([{"type": "function", "function": {"name": "x", "parameters": {}}}])


def test_streaming_refuses_a_provider_without_streaming():
    model = _model()
    assert model.provider.capabilities.supports_streaming is False
    with pytest.raises(LLMConfigurationError, match="does not support streaming"):
        list(model.stream([HumanMessage(content="q")]))


def test_structured_output_goes_through_the_projects_own_path():
    """The adapter must NOT re-derive JSON mode vs forced tool call."""
    provider = _ScriptedLLM(
        structured_responses={
            IntentClassification: [
                IntentClassification(category=IntentCategory.FILING_RESEARCH, reasoning="r")
            ]
        }
    )
    model = EDLChatModel(provider=provider)
    value = model.with_structured_output(IntentClassification).invoke(
        [HumanMessage(content="what did the 10-K say?")]
    )
    assert value.category is IntentCategory.FILING_RESEARCH
    # One call, through generate_structured -- not through generate+tools.
    assert len(provider.generate_structured_calls) == 1
    assert provider.generate_calls == []


def test_structured_output_rejects_a_non_pydantic_schema():
    with pytest.raises(LLMConfigurationError, match="pydantic BaseModel"):
        _model().with_structured_output({"type": "object"})


def test_provider_errors_are_not_wrapped():
    """Requirement 39: a provider failure must arrive as itself."""
    model = _model(generate_responses=[LLMRequestError("upstream 503")])
    with pytest.raises(LLMRequestError, match="upstream 503"):
        model.invoke([HumanMessage(content="q")])

    structured = EDLChatModel(
        provider=_ScriptedLLM(
            structured_responses={IntentClassification: [StructuredOutputError("bad json")]}
        )
    )
    with pytest.raises(StructuredOutputError, match="bad json"):
        structured.with_structured_output(IntentClassification).invoke(
            [HumanMessage(content="q")]
        )
