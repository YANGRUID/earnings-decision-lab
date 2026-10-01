"""The LangGraph research state.

Design rules, in the order they mattered:

1. **Auditable** (requirement 21). A checkpointed run must be able to
   answer "what did we know, when did we know it, which tool produced it,
   and which evidence supported the final answer?" -- so every evidence
   block names its producing tool and its category, and the node trace
   carries real timings rather than being reconstructed afterwards.

2. **JSON-safe**. Every value here is ``str``/``int``/``float``/``bool``/
   ``list``/``dict``/``None``. Dates and ``Decimal`` are stringified at the
   node boundary, not stored raw. A checkpoint is operational state that an
   operator may well have to read in psql at 15:40 on a window day, so it
   should be legible there, and this also keeps it independent of whichever
   serializer the checkpointer uses.

3. **Only fields a node actually reads or writes** (requirement 20). There
   is no ``confidence``, no ``embeddings``, no ``messages`` transcript --
   nothing here exists to look thorough.

Reducers: the five *append-only* lists use ``operator.add`` so a targeted
retrieval round adds to the record instead of overwriting it (and so the
parallel evidence nodes of Part J can merge without a custom reducer
later). ``evidence_blocks`` and ``citations`` are deliberately NOT
additive -- ``merge_evidence`` recomputes them from the full
``tool_records`` every time it runs, which keeps that node idempotent
across retrieval rounds instead of duplicating blocks.
"""

import operator
from typing import Annotated, Any, Literal, NotRequired, TypedDict

#: Runtime identities (requirement 6). Persisted with a run so provenance
#: says which engine produced it; historical rows are never rewritten.
AGENT_RUNTIME_LEGACY = "legacy-agent-v1"
AGENT_RUNTIME_LANGGRAPH = "langgraph-agent-v1"

#: Identity of the graph's own shape. Bump when nodes or edges change in a
#: way that changes what a run means -- not for an internal refactor.
RESEARCH_GRAPH_VERSION = "edl-research-graph-v1"

#: Bounds. Both are deliberately small and both are enforced in the
#: routing functions, not merely documented.
#: One revision, exactly matching the legacy orchestrator's behaviour
#: (agents/orchestrator.py::_verify_and_maybe_revise attempts it once).
MAX_REVISIONS = 1
#: One targeted retrieval round. Retrying the *missing category only* once
#: is the cheap half of the benefit; a second round has never been the
#: difference between answerable and not in the fixtures measured here, and
#: each round spends real SEC/DB/quota budget (Part S).
MAX_RETRIEVAL_ROUNDS = 1
#: Unchanged from the legacy orchestrator's MAX_TOOL_CALLS.
MAX_TOOL_CALLS = 6


class PlannedCall(TypedDict):
    tool_name: str
    arguments: dict[str, Any]


class ToolRecord(TypedDict):
    """Mirrors ``agents.types.ToolCallRecord`` as JSON.

    ``retrieval_round`` is the one addition: 0 for the initial plan, 1+ for
    a targeted retrieval, so the trace shows which evidence arrived only
    because the quality gate asked again.
    """

    tool_name: str
    arguments: dict[str, Any]
    success: bool
    duration_ms: float
    summary: str
    error: NotRequired[str | None]
    query_description: NotRequired[str | None]
    evidence_category: str
    retrieval_round: int


class EvidenceBlock(TypedDict):
    """One tool's contribution to the evidence handed to synthesis."""

    tool_name: str
    category: str
    text: str
    #: How many rows/chunks/records this block actually represents. The
    #: quality gate uses it to tell "succeeded with data" from "succeeded
    #: with nothing", which a success flag alone cannot express.
    item_count: int
    retrieval_round: int
    #: Set only by the consensus-estimates block, which is the one source
    #: that reports an expected report date. The quality gate compares it
    #: against the calendar event to answer "is the event timing
    #: corroborated?" -- carried on the block rather than passed around so
    #: the fact is visible in the checkpoint, not just in a function call.
    reported_earnings_date: NotRequired[str | None]
    #: Best retrieval score in this block, when the producing path exposes
    #: one. The filing-search TOOL does not return scores, so a round-0
    #: filing block leaves this None and the gate falls back to counting
    #: citations -- stated here rather than papered over with a fake score.
    top_score: NotRequired[float | None]


class CitationRef(TypedDict):
    """``rag.context.Citation`` as JSON. Field-for-field, so converting
    back at the API boundary is a rename-free round trip."""

    marker: str
    ticker: str
    filing_type: str
    filing_date: str
    section: str | None
    source_url: str
    accession_number: str | None
    evidence_cutoff: str | None


class NodeRun(TypedDict):
    """Operational trace for one node execution (requirement 41).

    No prompts, no model output, no chain-of-thought -- only what
    Operations needs to see where a run spent its time and what failed.
    """

    node: str
    started_at: str
    finished_at: str
    duration_ms: float
    status: Literal["ok", "degraded", "failed", "skipped"]
    llm_calls: int
    tool_calls: int
    attempt: int


class GraphError(TypedDict):
    """A failure, kept in its own domain terms (requirement 39/40).

    ``category`` is the real exception class name from
    ``services.llm.errors`` or the service that failed -- never collapsed
    into one ``AgentError``. ``recoverable`` says whether retrying the same
    node could plausibly help; ``checkpointed`` says whether a resume has
    somewhere to resume from.
    """

    node: str
    category: str
    message: str
    recoverable: bool
    checkpointed: bool
    occurred_at: str


class ResearchState(TypedDict, total=False):
    # --- identity and point in time -----------------------------------
    run_id: str
    question: str
    #: Real, already-resolved tickers from services/research_query_resolution.py
    #: -- never a guess the graph made.
    resolved_tickers: list[str]
    company_id: int | None
    earnings_calendar_event_id: int | None
    #: ISO date, or None for "as of now".
    as_of: str | None

    # --- planning ------------------------------------------------------
    intent: str
    intent_reasoning: str | None
    #: "native_tool_calling" | "structured_planner" -- the same capability
    #: branch the legacy orchestrator makes, preserved rather than assumed.
    planning_method: str
    plan: list[PlannedCall]

    # --- evidence ------------------------------------------------------
    tool_records: Annotated[list[ToolRecord], operator.add]
    evidence_blocks: list[EvidenceBlock]
    citations: list[CitationRef]
    #: ``schemas.agent.EvidenceQualityResult`` as a dict.
    evidence_quality: dict[str, Any] | None
    retrieval_attempt_count: int

    # --- synthesis and verification ------------------------------------
    answer: str
    #: ``schemas.agent.VerificationResult`` as a dict.
    verification: dict[str, Any] | None
    revision_count: int
    revised: bool

    # --- research freshness at the legal decision window (Part R) -------
    #: Populated only when the run names a real calendar event. Computed by
    #: reusing services/earnings_research_preparation.py -- the fix that
    #: judges freshness AT the window rather than at now is preserved by
    #: calling it, not by reimplementing it here.
    legal_decision_at: str | None
    thesis_expires_at: str | None
    needs_refresh_for_window: bool | None

    # --- operational trace ---------------------------------------------
    node_runs: Annotated[list[NodeRun], operator.add]
    errors: Annotated[list[GraphError], operator.add]
    warnings: Annotated[list[str], operator.add]
    llm_calls: int
    input_tokens: int
    output_tokens: int
    provider: str
    model: str
    runtime_version: str
    graph_version: str


def initial_state(
    *,
    run_id: str,
    question: str,
    resolved_tickers: list[str] | None = None,
    company_id: int | None = None,
    earnings_calendar_event_id: int | None = None,
    as_of: str | None = None,
    provider: str = "",
    model: str = "",
) -> ResearchState:
    """Every counter starts at a real zero, so a node never has to ask
    whether a key exists before adding to it."""
    return ResearchState(
        run_id=run_id,
        question=question,
        resolved_tickers=list(resolved_tickers or []),
        company_id=company_id,
        earnings_calendar_event_id=earnings_calendar_event_id,
        as_of=as_of,
        intent="",
        intent_reasoning=None,
        planning_method="",
        plan=[],
        tool_records=[],
        evidence_blocks=[],
        citations=[],
        evidence_quality=None,
        retrieval_attempt_count=0,
        answer="",
        verification=None,
        revision_count=0,
        revised=False,
        legal_decision_at=None,
        thesis_expires_at=None,
        needs_refresh_for_window=None,
        node_runs=[],
        errors=[],
        warnings=[],
        llm_calls=0,
        input_tokens=0,
        output_tokens=0,
        provider=provider,
        model=model,
        runtime_version=AGENT_RUNTIME_LANGGRAPH,
        graph_version=RESEARCH_GRAPH_VERSION,
    )
