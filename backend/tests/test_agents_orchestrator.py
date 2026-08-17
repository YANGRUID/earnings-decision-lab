"""Tests for the full agent orchestration pipeline: planning (both native
tool-calling and the structured-planner fallback), tool execution, evidence
collection, synthesis, verification/revision, execution traces, and
failure recovery at every stage.
"""

from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel

from agents.orchestrator import MAX_TOOL_CALLS, AgentOrchestrator
from models.company import Company
from models.document_chunk import EMBEDDING_DIM, DocumentChunk
from models.enums import FilingType
from models.filing import Filing
from schemas.agent import (
    IntentCategory,
    IntentClassification,
    ToolPlan,
    ToolPlanItem,
    VerificationResult,
)
from services.llm.base import LLMProvider
from services.llm.errors import LLMRequestError
from services.llm.types import Capabilities, GenerateResult, TokenUsage, ToolCall

NOW = datetime.now(UTC)


class _StubEmbedder:
    model_name = "stub"
    dimension = EMBEDDING_DIM

    def embed(self, texts):
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


class _ScriptedLLM(LLMProvider):
    """Returns pre-scripted responses in call order, separately queued per
    schema for generate_structured and in a flat queue for generate. Raises
    LLMRequestError once a queue is exhausted (or if a queued item is
    itself an exception instance) — used to test failure recovery.
    """

    name = "scripted"

    def __init__(
        self,
        model: str = "deepseek-v4-flash",
        supports_tool_calling: bool = True,
        structured_responses: dict | None = None,
        generate_responses: list | None = None,
    ) -> None:
        self.model = model
        self.capabilities = Capabilities(
            supports_structured_output=True,
            supports_tool_calling=supports_tool_calling,
            supports_streaming=False,
        )
        self._structured_queue: dict = defaultdict(list)
        for schema, items in (structured_responses or {}).items():
            self._structured_queue[schema] = list(items)
        self._generate_queue = list(generate_responses or [])
        self.generate_calls: list = []
        self.generate_structured_calls: list = []

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=1024):
        self.generate_calls.append((messages, tools))
        if not self._generate_queue:
            raise LLMRequestError("scripted generate() queue exhausted")
        item = self._generate_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def generate_structured(
        self, messages, schema: type[BaseModel], *, temperature=0.0, max_tokens=1024
    ):
        self.generate_structured_calls.append((messages, schema))
        queue = self._structured_queue[schema]
        if not queue:
            raise LLMRequestError(
                f"scripted generate_structured({schema.__name__}) queue exhausted"
            )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, messages, *, temperature=0.0, max_tokens=1024) -> Iterator[str]:
        raise NotImplementedError


def _default_intent():
    return {
        IntentClassification: [
            IntentClassification(category=IntentCategory.OPTIONS_ANALYTICS, reasoning="test")
        ]
    }


def test_native_tool_calling_full_pipeline_success(db_session):
    tool_call = ToolCall(
        id="call_1",
        name="calculate_strategy_payoff",
        arguments={
            "strategy_label": "long call",
            "legs": [{"option_type": "call", "action": "buy", "strike": "100", "premium": "5"}],
        },
    )
    llm = _ScriptedLLM(
        structured_responses={
            **_default_intent(),
            VerificationResult: [VerificationResult(supported=True)],
        },
        generate_responses=[
            GenerateResult(
                tool_calls=[tool_call], usage=TokenUsage(input_tokens=50, output_tokens=20)
            ),
            GenerateResult(
                content="The long call has max loss 5.",
                usage=TokenUsage(input_tokens=80, output_tokens=30),
            ),
        ],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("What's the max loss on a long call at strike 100, premium 5?")

    assert response.answer == "The long call has max loss 5."
    assert response.trace.planning_method == "native_tool_calling"
    assert len(response.trace.tool_calls) == 1
    assert response.trace.tool_calls[0].success is True
    assert response.trace.tool_calls[0].tool_name == "calculate_strategy_payoff"
    assert response.trace.verification_ran is True
    assert response.trace.verification_supported is True
    assert response.trace.revised is False
    assert response.trace.total_input_tokens == 130
    assert response.trace.total_output_tokens == 50
    assert response.trace.intent_category == "options_analytics"


def test_no_tool_needed_returns_direct_answer(db_session):
    llm = _ScriptedLLM(
        structured_responses={
            IntentClassification: [
                IntentClassification(category=IntentCategory.GENERAL, reasoning="greeting")
            ]
        },
        generate_responses=[GenerateResult(content="Hello! Ask me about NVDA, AMD, MU, or SNDK.")],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("hi there")

    assert response.answer == "Hello! Ask me about NVDA, AMD, MU, or SNDK."
    assert response.trace.tool_calls == []
    assert response.trace.verification_ran is False  # nothing to verify without evidence


def test_structured_planner_fallback_for_non_tool_calling_provider(db_session):
    plan = ToolPlan(
        items=[
            ToolPlanItem(
                tool_name="calculate_strategy_payoff",
                arguments={
                    "strategy_label": "long put",
                    "legs": [
                        {"option_type": "put", "action": "buy", "strike": "100", "premium": "4"}
                    ],
                },
            )
        ]
    )
    llm = _ScriptedLLM(
        supports_tool_calling=False,
        structured_responses={
            **_default_intent(),
            ToolPlan: [plan],
            VerificationResult: [VerificationResult(supported=True)],
        },
        generate_responses=[GenerateResult(content="Long put max loss is 4.")],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("Long put payoff?")

    assert response.trace.planning_method == "structured_planner"
    assert len(response.trace.tool_calls) == 1
    assert response.trace.tool_calls[0].success is True
    assert response.answer == "Long put max loss is 4."


def test_unknown_tool_recorded_as_failure_not_crash(db_session):
    tool_call = ToolCall(id="call_1", name="not_a_real_tool", arguments={})
    llm = _ScriptedLLM(
        structured_responses={
            **_default_intent(),
            VerificationResult: [VerificationResult(supported=True)],
        },
        generate_responses=[
            GenerateResult(tool_calls=[tool_call]),
            GenerateResult(content="I couldn't find that data."),
        ],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("some question")

    assert len(response.trace.tool_calls) == 1
    assert response.trace.tool_calls[0].success is False
    assert "unknown tool" in response.trace.tool_calls[0].error
    assert response.answer == "I couldn't find that data."  # pipeline still completes


def test_tool_argument_validation_failure_recorded_not_crash(db_session):
    # Missing required 'legs' field — Pydantic validation inside _execute_tool
    # must be caught, not propagate and crash the whole request.
    tool_call = ToolCall(
        id="call_1", name="calculate_strategy_payoff", arguments={"strategy_label": "x"}
    )
    llm = _ScriptedLLM(
        structured_responses={
            **_default_intent(),
            VerificationResult: [VerificationResult(supported=True)],
        },
        generate_responses=[
            GenerateResult(tool_calls=[tool_call]),
            GenerateResult(content="Missing data, cannot compute."),
        ],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("bad args question")

    assert response.trace.tool_calls[0].success is False
    assert response.trace.tool_calls[0].error is not None


def test_planning_llm_failure_degrades_gracefully(db_session):
    llm = _ScriptedLLM(
        structured_responses=_default_intent(),
        generate_responses=[],  # empty queue -> LLMRequestError on the planning call
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("any question")

    assert "temporarily unavailable" in response.answer
    assert response.trace.tool_calls == []


def test_verification_failure_triggers_one_revision(db_session):
    tool_call = ToolCall(
        id="call_1",
        name="calculate_strategy_payoff",
        arguments={
            "strategy_label": "long call",
            "legs": [{"option_type": "call", "action": "buy", "strike": "100", "premium": "5"}],
        },
    )
    llm = _ScriptedLLM(
        structured_responses={
            **_default_intent(),
            VerificationResult: [
                VerificationResult(supported=False, unsupported_claims=["max profit is $1000"])
            ],
        },
        generate_responses=[
            GenerateResult(tool_calls=[tool_call]),
            GenerateResult(content="Draft with an unsupported claim: max profit is $1000."),
            GenerateResult(content="Revised: max profit is unbounded."),
        ],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("payoff question")

    assert response.answer == "Revised: max profit is unbounded."
    assert response.trace.verification_supported is False
    assert response.trace.revised is True


def test_verification_llm_failure_degrades_without_revision(db_session):
    tool_call = ToolCall(
        id="call_1",
        name="calculate_strategy_payoff",
        arguments={
            "strategy_label": "long call",
            "legs": [{"option_type": "call", "action": "buy", "strike": "100", "premium": "5"}],
        },
    )
    llm = _ScriptedLLM(
        structured_responses={**_default_intent(), VerificationResult: []},  # exhausted -> raises
        generate_responses=[
            GenerateResult(tool_calls=[tool_call]),
            GenerateResult(content="Draft answer."),
        ],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("payoff question")

    assert response.answer == "Draft answer."
    assert response.trace.verification_ran is False
    assert response.trace.revised is False


def test_intent_classification_failure_degrades_to_general(db_session):
    llm = _ScriptedLLM(
        structured_responses={IntentClassification: []},  # exhausted -> raises immediately
        generate_responses=[GenerateResult(content="hello", tool_calls=[])],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("hi")

    assert response.trace.intent_category == "general"


def test_max_tool_calls_bound_is_respected(db_session):
    many_calls = [
        ToolCall(
            id=f"call_{i}",
            name="calculate_strategy_payoff",
            arguments={
                "strategy_label": "long call",
                "legs": [{"option_type": "call", "action": "buy", "strike": "100", "premium": "5"}],
            },
        )
        for i in range(MAX_TOOL_CALLS + 5)
    ]
    llm = _ScriptedLLM(
        structured_responses={
            **_default_intent(),
            VerificationResult: [VerificationResult(supported=True)],
        },
        generate_responses=[
            GenerateResult(tool_calls=many_calls),
            GenerateResult(content="ok"),
        ],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("spam question")

    assert len(response.trace.tool_calls) == MAX_TOOL_CALLS


def test_citations_flow_through_from_filings_search_tool(db_session):
    company = Company(ticker="ZZAGT8", name="ZZ Agent Test 8", cik="0009990008")
    db_session.add(company)
    db_session.flush()
    filing = Filing(
        company_id=company.id,
        filing_type=FilingType.FORM_10Q,
        filing_date=date(2025, 12, 18),
        accession_number="TEST-AGT-0099",
        source_url="https://example.com/zzagt8.htm",
        retrieved_at=NOW,
    )
    db_session.add(filing)
    db_session.flush()
    db_session.add(
        DocumentChunk(
            filing_id=filing.id,
            company_id=company.id,
            chunk_index=0,
            section="Item 7",
            text="Zzagt8 gross margin expanded due to favorable mix.",
            token_count=8,
            embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
            embedding_model="stub",
            retrieved_at=NOW,
        )
    )
    db_session.flush()

    tool_call = ToolCall(
        id="call_1",
        name="search_filings",
        arguments={"query": "gross margin", "ticker": "ZZAGT8"},
    )
    llm = _ScriptedLLM(
        structured_responses={
            **_default_intent(),
            VerificationResult: [VerificationResult(supported=True)],
        },
        generate_responses=[
            GenerateResult(tool_calls=[tool_call]),
            GenerateResult(content="Gross margin expanded [1]."),
        ],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("What happened to ZZAGT8 gross margin?")

    assert len(response.citations) == 1
    assert response.citations[0].ticker == "ZZAGT8"


def test_cost_estimated_for_recognized_model(db_session):
    llm = _ScriptedLLM(
        model="deepseek-v4-flash",
        structured_responses={
            IntentClassification: [
                IntentClassification(category=IntentCategory.GENERAL, reasoning="x")
            ]
        },
        generate_responses=[
            GenerateResult(
                content="hi",
                tool_calls=[],
                usage=TokenUsage(input_tokens=1000, output_tokens=500),
            )
        ],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("hi")

    assert response.trace.estimated_cost_usd is not None
    assert response.trace.estimated_cost_usd > Decimal(0)


def test_cost_not_estimated_for_unrecognized_model(db_session):
    llm = _ScriptedLLM(
        model="some-unlisted-model",
        structured_responses={
            IntentClassification: [
                IntentClassification(category=IntentCategory.GENERAL, reasoning="x")
            ]
        },
        generate_responses=[
            GenerateResult(
                content="hi",
                tool_calls=[],
                usage=TokenUsage(input_tokens=1000, output_tokens=500),
            )
        ],
    )
    orchestrator = AgentOrchestrator(db_session, llm, _StubEmbedder())

    response = orchestrator.run("hi")

    assert response.trace.estimated_cost_usd is None
