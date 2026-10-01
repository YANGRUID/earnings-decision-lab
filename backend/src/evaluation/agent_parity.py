"""Legacy vs LangGraph structural parity (requirements 44-48).

Compares the two runtimes on the SAME question, the SAME seeded database
and the SAME scripted provider -- a comparison in which each side gets its
own fake proves only that two fakes agree.

What is compared (requirement 45) is *structural and behavioural*: the
intent, the plan, which tools were offered and called, whether the
structured outputs validated, whether citations came through, whether
verification ran and what it concluded, the revision count, and whether the
final answer is non-empty. Prose is NOT compared (requirement 46) -- an LLM
at temperature 0.0 is reproducible for a scripted fake but not in general,
and demanding identical wording would turn a true parity pass into a
flake.

The budgets (requirements 47, 48) are counted rather than reasoned about:
``llm_calls`` and ``tool_calls`` per side, so "2 calls became 12" would be
a number in a table rather than a thing someone noticed later.
"""

from dataclasses import asdict, dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from agents.graph.runtime import run_research_graph
from agents.orchestrator import AgentOrchestrator
from agents.types import AgentResponse
from rag.embeddings import EmbeddingProvider
from services.llm.base import LLMProvider


@dataclass(frozen=True)
class RuntimeObservation:
    """What one runtime did, in terms both runtimes can express."""

    runtime: str
    intent_category: str
    planning_method: str
    tools_called: list[str]
    tools_succeeded: list[str]
    citation_count: int
    verification_ran: bool
    verification_supported: bool | None
    revised: bool
    answer_present: bool
    llm_calls: int
    tool_calls: int
    #: LangGraph only. None for legacy, which has no gate -- reported as
    #: an addition rather than as a difference, because an absent feature
    #: is not a parity failure.
    evidence_quality_status: str | None = None
    retrieval_rounds: int | None = None

    def comparable(self) -> dict[str, object]:
        """Every field the two runtimes are compared on.

        Excludes the answer text (requirement 46), the timings, and the
        gate itself -- legacy has no gate, and an absent feature is an
        addition, not a divergence.
        """
        return {
            "intent_category": self.intent_category,
            "planning_method": self.planning_method,
            "tools_called": sorted(self.tools_called),
            "tools_succeeded": sorted(self.tools_succeeded),
            "citation_count": self.citation_count,
            "verification_ran": self.verification_ran,
            "verification_supported": self.verification_supported,
            "revised": self.revised,
            "answer_present": self.answer_present,
        }


@dataclass(frozen=True)
class ParityCase:
    """One corpus entry.

    ``llm_factory`` is a callable rather than an instance because each side
    must start from an identical, unconsumed script -- a shared instance
    would let the first runtime drain the queue.
    """

    name: str
    question: str
    resolved_tickers: list[str] = field(default_factory=list)
    company_id: int | None = None
    as_of: date | None = None


#: Differences the LangGraph runtime is *designed* to produce, each with
#: the condition under which it is allowed and why.
#:
#: This distinction is the whole point of the harness. A difference that is
#: declared and whose precondition actually held on this run is the new
#: capability working; the same difference on a run where the precondition
#: did NOT hold is a regression. Silently excluding these fields from the
#: comparison would have hidden both.
DECLARED_DIFFERENCES: dict[str, tuple[str, str]] = {
    "tools_called": (
        "retrieval_rounds",
        "the evidence-quality gate asked a follow-up question for a missing or thin "
        "category, which the legacy runtime has no mechanism to ask",
    ),
    "tools_succeeded": (
        "retrieval_rounds",
        "same follow-up: a tool that only ran because the gate asked for it",
    ),
    "citation_count": (
        "retrieval_rounds",
        "the targeted filing retrieval returned citations the first pass did not",
    ),
    "verification_supported": (
        "revised",
        "the graph re-verifies a revised answer (requirement 23's revise -> verify "
        "edge), so it reports the verdict on the answer actually returned; legacy "
        "revises and then reports the PRE-revision verdict, describing an answer it "
        "already replaced",
    ),
}


def _precondition_held(observation: RuntimeObservation, field_name: str) -> bool:
    if field_name == "retrieval_rounds":
        return bool(observation.retrieval_rounds)
    if field_name == "revised":
        return observation.revised
    raise ValueError(f"unknown parity precondition {field_name!r}")


@dataclass(frozen=True)
class ParityResult:
    case: str
    legacy: RuntimeObservation
    langgraph: RuntimeObservation
    #: Differences the graph is designed to produce AND whose precondition
    #: genuinely held on this run. Reported, not failed.
    declared: list[str]
    #: Everything else that differs. Any entry here is a real regression.
    unexpected: list[str]
    llm_call_delta: int
    tool_call_delta: int

    @property
    def passed(self) -> bool:
        return not self.unexpected

    @property
    def differences(self) -> list[str]:
        return self.declared + self.unexpected

    def as_dict(self) -> dict:
        return {
            "case": self.case,
            "passed": self.passed,
            "declared_differences": [
                {"field": f, "because": DECLARED_DIFFERENCES[f][1]} for f in self.declared
            ],
            "unexpected_differences": self.unexpected,
            "llm_call_delta": self.llm_call_delta,
            "tool_call_delta": self.tool_call_delta,
            "legacy": asdict(self.legacy),
            "langgraph": asdict(self.langgraph),
        }


def _observe_legacy(response: AgentResponse, llm_calls: int) -> RuntimeObservation:
    trace = response.trace
    return RuntimeObservation(
        runtime="legacy-agent-v1",
        intent_category=trace.intent_category,
        planning_method=trace.planning_method,
        tools_called=[tc.tool_name for tc in trace.tool_calls],
        tools_succeeded=[tc.tool_name for tc in trace.tool_calls if tc.success],
        citation_count=len(response.citations),
        verification_ran=trace.verification_ran,
        verification_supported=trace.verification_supported,
        revised=trace.revised,
        answer_present=bool(response.answer.strip()),
        llm_calls=llm_calls,
        tool_calls=len(trace.tool_calls),
    )


def run_parity_case(
    db: Session,
    case: ParityCase,
    *,
    legacy_llm: LLMProvider,
    graph_llm: LLMProvider,
    embedder: EmbeddingProvider,
) -> ParityResult:
    """Runs both runtimes and diffs their comparable subsets.

    ``legacy_llm`` and ``graph_llm`` must be two independently-constructed
    providers loaded with the same script.
    """
    legacy_response = AgentOrchestrator(db, legacy_llm, embedder).run(
        case.question,
        resolved_tickers=case.resolved_tickers or None,
        as_of=case.as_of,
    )
    legacy = _observe_legacy(legacy_response, _counted_calls(legacy_llm))

    graph_result = run_research_graph(
        db,
        graph_llm,
        embedder,
        case.question,
        resolved_tickers=case.resolved_tickers or None,
        company_id=case.company_id,
        as_of=case.as_of,
    )
    graph_trace = graph_result.response.trace
    quality = graph_result.evidence_quality or {}
    langgraph = RuntimeObservation(
        runtime=graph_result.runtime_version,
        intent_category=graph_trace.intent_category,
        planning_method=graph_trace.planning_method,
        tools_called=[tc.tool_name for tc in graph_trace.tool_calls],
        tools_succeeded=[tc.tool_name for tc in graph_trace.tool_calls if tc.success],
        citation_count=len(graph_result.response.citations),
        verification_ran=graph_trace.verification_ran,
        verification_supported=graph_trace.verification_supported,
        revised=graph_trace.revised,
        answer_present=bool(graph_result.response.answer.strip()),
        llm_calls=graph_result.llm_calls,
        tool_calls=len(graph_trace.tool_calls),
        evidence_quality_status=quality.get("status"),
        retrieval_rounds=graph_result.retrieval_rounds,
    )

    left, right = legacy.comparable(), langgraph.comparable()
    declared: list[str] = []
    unexpected: list[str] = []
    for key in left:
        if left[key] == right[key]:
            continue
        precondition = DECLARED_DIFFERENCES.get(key)
        if precondition is not None and _precondition_held(langgraph, precondition[0]):
            declared.append(key)
        else:
            unexpected.append(key)
    return ParityResult(
        case=case.name,
        legacy=legacy,
        langgraph=langgraph,
        declared=declared,
        unexpected=unexpected,
        llm_call_delta=langgraph.llm_calls - legacy.llm_calls,
        tool_call_delta=langgraph.tool_calls - legacy.tool_calls,
    )


def _counted_calls(llm: LLMProvider) -> int:
    """The legacy orchestrator does not report a call count, so it is read
    off the provider. Works for any provider that records its calls (the
    scripted fake does); real providers report usage per call instead,
    which the trace already carries."""
    structured = len(getattr(llm, "generate_structured_calls", []))
    plain = len(getattr(llm, "generate_calls", []))
    return structured + plain


def summarize(results: list[ParityResult]) -> dict:
    """The shape the final report's parity table is built from."""
    return {
        "cases": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": [r.case for r in results if not r.passed],
        "cases_with_declared_differences": [r.case for r in results if r.declared],
        "max_llm_call_delta": max((r.llm_call_delta for r in results), default=0),
        "max_tool_call_delta": max((r.tool_call_delta for r in results), default=0),
        "results": [r.as_dict() for r in results],
    }
