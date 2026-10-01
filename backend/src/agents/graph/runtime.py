"""Running the research graph, and turning its state into an AgentResponse.

The API contract does not change (requirement 51's rollback story depends
on that): this returns the same ``agents.types.AgentResponse`` the legacy
orchestrator returns, so ``POST /research/query`` serialises either runtime
through one response model. The graph-only extras -- evidence quality, the
node trace, the bounded-loop counters -- ride alongside in
``GraphRunResult`` for the diagnostics view, and are simply absent when
the legacy runtime answered.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from agents.adapters.model import EDLChatModel
from agents.adapters.tools import build_langchain_tools
from agents.cost import estimate_cost_usd
from agents.graph.deps import GraphDeps
from agents.graph.nodes import new_run_id
from agents.graph.state import (
    AGENT_RUNTIME_LANGGRAPH,
    RESEARCH_GRAPH_VERSION,
    CitationRef,
    GraphError,
    NodeRun,
    ResearchState,
    initial_state,
)
from agents.graph.workflow import build_research_graph
from agents.types import AgentResponse, ExecutionTrace, ToolCallRecord
from rag.context import Citation
from rag.embeddings import EmbeddingProvider
from schemas.agent import IntentCategory
from services.llm.base import LLMProvider
from services.llm.types import TokenUsage


@dataclass(frozen=True)
class GraphRunResult:
    response: AgentResponse
    run_id: str
    runtime_version: str
    graph_version: str
    #: ``schemas.agent.EvidenceQualityResult`` as a dict, or None when the
    #: gate never ran (a run that failed at planning).
    evidence_quality: dict | None
    node_runs: list[NodeRun]
    errors: list[GraphError]
    warnings: list[str]
    retrieval_rounds: int
    revision_count: int
    llm_calls: int
    checkpoint_thread_id: str | None


def checkpoint_thread_id(
    *,
    run_id: str | None = None,
    company_id: int | None = None,
    earnings_calendar_event_id: int | None = None,
    as_of: date | None = None,
) -> str:
    """A deterministic, collision-free checkpoint identity (requirement 36).

    Two genuinely different shapes of run, named differently on purpose:

    - ``research:{run_id}`` -- one interactive question. A resume means
      "finish answering THIS question", so the run's own id is the identity
      and a new question is a new thread rather than a continuation of an
      unrelated one.
    - ``prep:{company_id}:{event_id}:{as_of}`` -- preparation for one
      company's one event at one point in time. Deterministic so a retry
      of the SAME preparation resumes instead of starting over, which is
      the whole point of requirement 35. ``as_of`` is in the key because
      the same event prepared at a different cutoff is a different run,
      and sharing a thread would let one overwrite the other's state.

    The two namespaces cannot collide: a UUID never contains a colon.
    """
    if earnings_calendar_event_id is not None:
        cutoff = as_of.isoformat() if as_of else "now"
        company = company_id if company_id is not None else "-"
        return f"prep:{company}:{earnings_calendar_event_id}:{cutoff}"
    if run_id is None:
        raise ValueError("a checkpoint thread needs either a run_id or an earnings event")
    return f"research:{run_id}"


def run_research_graph(
    db: Session,
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    question: str,
    *,
    resolved_tickers: list[str] | None = None,
    as_of: date | None = None,
    company_id: int | None = None,
    earnings_calendar_event_id: int | None = None,
    checkpointer: object | None = None,
    run_id: str | None = None,
) -> GraphRunResult:
    model = EDLChatModel(provider=llm)
    _, registry = build_langchain_tools(db, embedder)
    deps = GraphDeps(db=db, model=model, embedder=embedder, registry=registry)

    resolved_run_id = run_id or new_run_id()
    state = initial_state(
        run_id=resolved_run_id,
        question=question,
        resolved_tickers=resolved_tickers,
        company_id=company_id,
        earnings_calendar_event_id=earnings_calendar_event_id,
        as_of=as_of.isoformat() if as_of else None,
        provider=llm.name,
        model=llm.model,
    )

    thread_id: str | None = None
    config: dict | None = None
    if checkpointer is not None:
        thread_id = checkpoint_thread_id(
            run_id=resolved_run_id,
            company_id=company_id,
            earnings_calendar_event_id=earnings_calendar_event_id,
            as_of=as_of,
        )
        config = {"configurable": {"thread_id": thread_id}}

    graph = build_research_graph(deps, checkpointer=checkpointer)
    final: ResearchState = graph.invoke(state, config=config)  # type: ignore[arg-type]
    return _to_result(final, llm, thread_id)


def resume_research_graph(
    db: Session,
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    *,
    checkpointer: object,
    thread_id: str,
) -> GraphRunResult:
    """Resumes a checkpointed run from wherever it stopped (requirement 35).

    ``invoke(None, config)`` is LangGraph's own resume: it reads the
    checkpointed state and continues from the next pending node, so the
    nodes that already completed are not re-executed (requirement 74).
    """
    model = EDLChatModel(provider=llm)
    _, registry = build_langchain_tools(db, embedder)
    deps = GraphDeps(db=db, model=model, embedder=embedder, registry=registry)
    graph = build_research_graph(deps, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    final: ResearchState = graph.invoke(None, config=config)  # type: ignore[arg-type]
    return _to_result(final, llm, thread_id)


def _to_result(
    state: ResearchState, llm: LLMProvider, thread_id: str | None
) -> GraphRunResult:
    verification = state.get("verification")
    node_runs = state.get("node_runs", [])
    total_duration_ms = sum(n["duration_ms"] for n in node_runs)
    input_tokens = state.get("input_tokens", 0)
    output_tokens = state.get("output_tokens", 0)
    usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    cost = (
        estimate_cost_usd(llm.model, usage) if (input_tokens or output_tokens) else None
    )

    trace = ExecutionTrace(
        intent_category=state.get("intent") or IntentCategory.GENERAL.value,
        planning_method=state.get("planning_method") or "",
        tool_calls=[
            ToolCallRecord(
                tool_name=r["tool_name"],
                arguments=r.get("arguments") or {},
                success=r["success"],
                duration_ms=r["duration_ms"],
                summary=r["summary"],
                error=r.get("error"),
                query_description=r.get("query_description"),
            )
            for r in state.get("tool_records", [])
        ],
        verification_ran=verification is not None,
        verification_supported=(
            bool(verification.get("supported")) if verification is not None else None
        ),
        revised=bool(state.get("revised")),
        model=state.get("model") or llm.model,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        estimated_cost_usd=cost,
        total_duration_ms=total_duration_ms,
    )
    response = AgentResponse(
        question=state["question"],
        answer=state.get("answer", ""),
        citations=[_to_citation(c) for c in state.get("citations", [])],
        trace=trace,
    )
    return GraphRunResult(
        response=response,
        run_id=state.get("run_id", ""),
        runtime_version=state.get("runtime_version") or AGENT_RUNTIME_LANGGRAPH,
        graph_version=state.get("graph_version") or RESEARCH_GRAPH_VERSION,
        evidence_quality=state.get("evidence_quality"),
        node_runs=list(node_runs),
        errors=list(state.get("errors", [])),
        warnings=list(state.get("warnings", [])),
        retrieval_rounds=state.get("retrieval_attempt_count", 0),
        revision_count=state.get("revision_count", 0),
        llm_calls=state.get("llm_calls", 0),
        checkpoint_thread_id=thread_id,
    )


def _to_citation(ref: CitationRef) -> Citation:
    cutoff = ref.get("evidence_cutoff")
    return Citation(
        marker=ref["marker"],
        ticker=ref["ticker"],
        filing_type=ref["filing_type"],
        filing_date=date.fromisoformat(ref["filing_date"]),
        section=ref.get("section"),
        source_url=ref["source_url"],
        accession_number=ref.get("accession_number"),
        evidence_cutoff=date.fromisoformat(cutoff) if cutoff else None,
    )
