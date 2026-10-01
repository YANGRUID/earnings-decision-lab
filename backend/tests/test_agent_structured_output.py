"""Bounded, observable structured output (requirements 68, 12).

The rule being protected: a schema failure must never quietly become a
default judgement. These tests assert the absence of a fallback as much as
the presence of a bound.
"""

from _agent_fakes import _ScriptedLLM
from langchain_core.messages import HumanMessage

from agents.adapters.model import EDLChatModel
from agents.adapters.structured import (
    MAX_STRUCTURED_ATTEMPTS,
    generate_structured_bounded,
)
from schemas.agent import IntentCategory, IntentClassification, VerificationResult
from services.llm.errors import MissingAPIKeyError, StructuredOutputError

MESSAGES = [HumanMessage(content="what did the latest 10-K say about margins?")]


def _run(queue, schema=IntentClassification, **kwargs):
    provider = _ScriptedLLM(structured_responses={schema: queue})
    outcome = generate_structured_bounded(
        EDLChatModel(provider=provider), MESSAGES, schema, **kwargs
    )
    return outcome, provider


def test_a_valid_response_is_returned_on_the_first_attempt():
    outcome, provider = _run(
        [IntentClassification(category=IntentCategory.FILING_RESEARCH, reasoning="r")]
    )
    assert outcome.ok
    assert outcome.value is not None
    assert outcome.value.category is IntentCategory.FILING_RESEARCH
    assert outcome.attempts == 1
    assert len(provider.generate_structured_calls) == 1


def test_one_bad_response_then_a_good_one_succeeds_on_the_retry():
    outcome, provider = _run(
        [
            StructuredOutputError("not json"),
            IntentClassification(category=IntentCategory.GENERAL, reasoning="r"),
        ]
    )
    assert outcome.ok
    assert outcome.attempts == 2
    assert len(provider.generate_structured_calls) == 2


def test_retry_is_bounded_and_the_failure_is_observable():
    outcome, provider = _run([StructuredOutputError("bad 1"), StructuredOutputError("bad 2")])
    assert not outcome.ok
    assert outcome.value is None
    assert outcome.attempts == MAX_STRUCTURED_ATTEMPTS == 2
    assert len(provider.generate_structured_calls) == MAX_STRUCTURED_ATTEMPTS
    assert outcome.error == "bad 2"
    assert outcome.error_category == "StructuredOutputError"


def test_a_parse_failure_never_becomes_a_default_judgement():
    """Requirement 12, stated as the absence it demands.

    IntentClassification has no default category and VerificationResult no
    default `supported`, so a failed outcome cannot carry one.
    """
    outcome, _ = _run([StructuredOutputError("x"), StructuredOutputError("x")])
    assert outcome.value is None

    verification, _ = _run(
        [StructuredOutputError("x"), StructuredOutputError("x")], schema=VerificationResult
    )
    assert verification.value is None
    # And the schemas themselves must not acquire a default later.
    assert IntentClassification.model_fields["category"].is_required()
    assert VerificationResult.model_fields["supported"].is_required()


def test_a_transport_failure_is_not_retried():
    """A missing key is not a parse problem; retrying spends quota to get
    the same answer (requirements 39, 62)."""
    outcome, provider = _run([MissingAPIKeyError("no key configured")])
    assert not outcome.ok
    assert outcome.attempts == 1
    assert len(provider.generate_structured_calls) == 1
    assert outcome.error_category == "MissingAPIKeyError"


def test_the_decision_view_schema_round_trips_through_the_adapter():
    """Requirement 54: the graph must be CAPABLE of producing the canonical
    DecisionView -- used for parity only, never on the forward path in
    LG-1 (asserted separately in test_agent_runtime_isolation.py)."""
    from schemas.decision import DecisionView

    view = DecisionView(
        direction="neutral",
        volatility_view="neutral_vol",
        rationale="r",
        bull_case="b",
        bear_case="b",
        key_catalysts="c",
        key_risks="k",
        disclaimer="Not investment advice.",
    )
    outcome, _ = _run([view], schema=DecisionView)
    assert outcome.ok
    assert outcome.value is not None
    assert outcome.value.direction == "neutral"
