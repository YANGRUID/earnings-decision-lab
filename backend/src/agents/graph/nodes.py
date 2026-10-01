"""The research graph's nodes.

Each node is a plain function of ``(state, deps) -> partial state``. Nothing
here is a method on a class holding hidden mutable state, because the whole
value of this refactor is that a run can be reconstructed from its
checkpoint -- which only holds if every effect a node has is visible in what
it returns.

Behavioural parity is kept by *reuse*, not by imitation: the prompts come
from ``prompts/agent_*``, the deterministic argument defaulting and the
evidence-block format come from ``agents/evidence.py``, and both runtimes
import the same ones. See docs/agentic_research_architecture.md.

Failure policy, matching the legacy orchestrator exactly:
- intent classification and verification are best-effort (degrade, never
  fail the run);
- a tool that raises is recorded as a failed call and synthesis is told
  about the gap;
- planning that cannot reach the provider ends the run with an honest
  message rather than an empty answer.
"""

import time
import uuid
from datetime import UTC, date, datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.adapters.retriever import EDLHybridRetriever, RetrievalScope
from agents.adapters.structured import generate_structured_bounded
from agents.adapters.tools import TOOL_EVIDENCE_CATEGORY, EvidenceCategory
from agents.evidence import apply_deterministic_defaults, evidence_block_text
from agents.graph.deps import GraphDeps
from agents.graph.quality import assess_evidence
from agents.graph.state import (
    MAX_TOOL_CALLS,
    CitationRef,
    EvidenceBlock,
    GraphError,
    NodeRun,
    PlannedCall,
    ResearchState,
    ToolRecord,
)
from agents.types import ToolCallRecord
from observability.redact import redact
from prompts.agent_intent import SYSTEM_PROMPT as INTENT_SYSTEM_PROMPT
from prompts.agent_planning import build_structured_planner_prompt, build_tool_calling_system_prompt
from prompts.agent_synthesis import SYSTEM_PROMPT as SYNTHESIS_SYSTEM_PROMPT
from prompts.agent_synthesis import build_synthesis_user_prompt
from prompts.agent_verification import SYSTEM_PROMPT as VERIFICATION_SYSTEM_PROMPT
from prompts.agent_verification import build_verification_user_prompt
from rag.context import Citation
from schemas.agent import IntentCategory, IntentClassification, ToolPlan, VerificationResult
from services.llm.errors import LLMError
from services.llm.types import ChatMessage

# --- node bookkeeping ---------------------------------------------------


class _Timer:
    """Produces the NodeRun entry for one node execution.

    Exists so no node has to remember to record itself: a node that forgets
    is a hole in the trace, and a trace with holes is worse than no trace
    because it looks complete.
    """

    def __init__(self, node: str, attempt: int = 1) -> None:
        self.node = node
        self.attempt = attempt
        self._start = time.monotonic()
        self._started_at = datetime.now(UTC)
        self.llm_calls = 0
        self.tool_calls = 0

    def done(self, status: str) -> NodeRun:
        finished = datetime.now(UTC)
        return NodeRun(
            node=self.node,
            started_at=self._started_at.isoformat(),
            finished_at=finished.isoformat(),
            duration_ms=(time.monotonic() - self._start) * 1000,
            status=status,  # type: ignore[typeddict-item]
            llm_calls=self.llm_calls,
            tool_calls=self.tool_calls,
            attempt=self.attempt,
        )


def _error(
    node: str, exc: Exception, *, recoverable: bool, checkpointed: bool = True
) -> GraphError:
    """Keeps the real exception class name (requirement 39). ``redact``
    strips credential-shaped substrings, which matters because a provider
    error can echo a request URL."""
    return GraphError(
        node=node,
        category=type(exc).__name__,
        message=redact(str(exc)),
        recoverable=recoverable,
        checkpointed=checkpointed,
        occurred_at=datetime.now(UTC).isoformat(),
    )


def _as_of_date(state: ResearchState) -> date | None:
    raw = state.get("as_of")
    return date.fromisoformat(raw) if raw else None


def _citation_ref(citation: Citation) -> CitationRef:
    return CitationRef(
        marker=citation.marker,
        ticker=citation.ticker,
        filing_type=citation.filing_type,
        filing_date=citation.filing_date.isoformat(),
        section=citation.section,
        source_url=citation.source_url,
        accession_number=citation.accession_number,
        evidence_cutoff=(
            citation.evidence_cutoff.isoformat() if citation.evidence_cutoff else None
        ),
    )


def _item_count(tool_name: str, data: dict[str, Any], citations: list[Citation]) -> int:
    """How much real data a successful tool call actually produced.

    Per-tool because each payload has its own shape -- and necessary
    because ``success=True`` with zero rows is this project's honest "no
    data available" answer (see agents/tools/base.py), which a success flag
    alone cannot distinguish from a full result.
    """
    if tool_name == "search_filings":
        return len(citations)
    if tool_name == "get_historical_earnings":
        return len(data.get("events") or [])
    if tool_name == "get_options_snapshot":
        return len(data.get("snapshots") or [])
    if tool_name == "get_analyst_estimates":
        return 1 if data.get("available") else 0
    if tool_name == "compare_guidance":
        # A real comparison carries the two filing dates it compared; a
        # "need at least 2 extractions" outcome carries only a count.
        return 1 if data.get("current_filing_date") else 0
    return 1 if data else 0


def _reported_earnings_date(tool_name: str, data: dict[str, Any]) -> str | None:
    if tool_name != "get_analyst_estimates":
        return None
    value = data.get("estimated_report_date")
    return str(value) if value else None


# --- Stage 1: intent classification (best-effort) -----------------------


def classify_intent(state: ResearchState, deps: GraphDeps) -> ResearchState:
    timer = _Timer("classify_intent")
    messages = [
        SystemMessage(content=INTENT_SYSTEM_PROMPT),
        HumanMessage(content=state["question"]),
    ]
    outcome = generate_structured_bounded(
        deps.model, messages, IntentClassification, temperature=0.0, max_tokens=200
    )
    timer.llm_calls = outcome.attempts
    if not outcome.ok:
        # Degrades exactly as the legacy orchestrator does: an unclassified
        # question is answered as GENERAL rather than refused.
        return ResearchState(
            intent=IntentCategory.GENERAL.value,
            intent_reasoning=None,
            llm_calls=state.get("llm_calls", 0) + outcome.attempts,
            node_runs=[timer.done("degraded")],
            warnings=[
                "Intent classification was unavailable, so the question was handled as a "
                "general research question."
            ],
            errors=[
                GraphError(
                    node="classify_intent",
                    category=outcome.error_category or "StructuredOutputError",
                    message=redact(outcome.error or ""),
                    recoverable=True,
                    checkpointed=True,
                    occurred_at=datetime.now(UTC).isoformat(),
                )
            ],
        )
    assert outcome.value is not None
    return ResearchState(
        intent=outcome.value.category.value,
        intent_reasoning=outcome.value.reasoning,
        llm_calls=state.get("llm_calls", 0) + outcome.attempts,
        node_runs=[timer.done("ok")],
    )


# --- Stage 2: research freshness at the legal decision window (Part R) ---


def window_context(state: ResearchState, deps: GraphDeps) -> ResearchState:
    """Deterministic, zero LLM calls, and a no-op unless this run names a
    real calendar event.

    Preserves the 2026-09-24 fix (requirement 59) by *calling* it: freshness
    is judged at the event's own legal decision window, not at now, via
    ``services.earnings_research_preparation.v4_research_ready(as_of=...)``.
    Reimplementing that comparison here is exactly how the two would drift
    apart again.
    """
    timer = _Timer("window_context")
    event_id = state.get("earnings_calendar_event_id")
    if event_id is None or not state.get("resolved_tickers"):
        return ResearchState(node_runs=[timer.done("skipped")])

    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415
    from services.earnings_research_preparation import (  # noqa: PLC0415
        legal_decision_window,
        v4_research_ready,
    )
    from services.research_orchestration import THESIS_FRESHNESS_DAYS  # noqa: PLC0415

    event = (
        deps.db.query(EarningsCalendarEvent)
        .filter(EarningsCalendarEvent.id == event_id)
        .one_or_none()
    )
    if event is None:
        return ResearchState(
            node_runs=[timer.done("skipped")],
            warnings=[f"No earnings calendar event {event_id} is on record."],
        )

    window = legal_decision_window(event)
    now = datetime.now(UTC)
    ready_now, _ = v4_research_ready(deps.db, state["resolved_tickers"][0], now=now)
    ready_at_window, reason = v4_research_ready(
        deps.db, state["resolved_tickers"][0], now=now, as_of=window
    )
    expires_at = _thesis_expiry(deps.db, state["resolved_tickers"][0], THESIS_FRESHNESS_DAYS)

    warnings: list[str] = []
    if ready_now and not ready_at_window:
        # The genuinely useful case: fresh when asked, stale when it
        # matters. Reported, never acted on -- refreshing research is the
        # preparation pipeline's job, not this graph's.
        warnings.append(
            f"This research is current now but {reason} -- it will need refreshing before "
            f"the decision window at {window.isoformat()}."
        )

    return ResearchState(
        legal_decision_at=window.isoformat(),
        thesis_expires_at=expires_at,
        needs_refresh_for_window=not ready_at_window,
        node_runs=[timer.done("ok")],
        warnings=warnings,
    )


def _thesis_expiry(db: Any, ticker: str, freshness_days: int) -> str | None:
    from datetime import timedelta  # noqa: PLC0415

    from models.ai_thesis_version import AIThesisVersion  # noqa: PLC0415
    from models.company import Company  # noqa: PLC0415

    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        return None
    latest = (
        db.query(AIThesisVersion)
        .filter(AIThesisVersion.company_id == company.id)
        .order_by(AIThesisVersion.created_at.desc())
        .first()
    )
    if latest is None:
        return None
    return (latest.created_at + timedelta(days=freshness_days)).isoformat()


# --- Stage 3: planning --------------------------------------------------


def plan_research(state: ResearchState, deps: GraphDeps) -> ResearchState:
    """Branches on the provider's real capability, same as the legacy
    orchestrator (``supports_tool_calling``) -- the graph does not assume
    every provider can call tools natively."""
    if deps.model.provider.capabilities.supports_tool_calling:
        return _plan_native(state, deps)
    return _plan_structured(state, deps)


def _plan_native(state: ResearchState, deps: GraphDeps) -> ResearchState:
    timer = _Timer("plan_research")
    tickers = state.get("resolved_tickers") or None
    messages = [
        ChatMessage(role="system", content=build_tool_calling_system_prompt(tickers)),
        ChatMessage(role="user", content=state["question"]),
    ]
    try:
        result = deps.model.provider.generate(
            messages,
            tools=[t.to_definition() for t in deps.registry.values()],
            temperature=0.0,
            max_tokens=1024,
        )
    except LLMError as exc:
        timer.llm_calls = 1
        return ResearchState(
            planning_method="native_tool_calling",
            plan=[],
            answer=f"The research assistant is temporarily unavailable ({exc}).",
            node_runs=[timer.done("failed")],
            errors=[_error("plan_research", exc, recoverable=True)],
        )
    timer.llm_calls = 1
    usage = result.usage
    plan: list[PlannedCall] = [
        PlannedCall(tool_name=tc.name, arguments=tc.arguments)
        for tc in result.tool_calls[:MAX_TOOL_CALLS]
    ]
    return ResearchState(
        planning_method="native_tool_calling",
        plan=plan,
        # A provider that answered without calling any tool has produced a
        # draft; keep it, exactly as the legacy path does.
        answer=(result.content or "") if not plan else "",
        llm_calls=state.get("llm_calls", 0) + 1,
        input_tokens=state.get("input_tokens", 0) + (usage.input_tokens if usage else 0),
        output_tokens=state.get("output_tokens", 0) + (usage.output_tokens if usage else 0),
        node_runs=[timer.done("ok")],
    )


def _plan_structured(state: ResearchState, deps: GraphDeps) -> ResearchState:
    import json  # noqa: PLC0415

    timer = _Timer("plan_research")
    catalog = "\n".join(
        f"- {t.name}: {t.description}\n"
        f"  args schema: {json.dumps(t.args_schema.model_json_schema())}"
        for t in deps.registry.values()
    )
    tickers = state.get("resolved_tickers") or None
    messages = [
        SystemMessage(content=build_structured_planner_prompt(catalog, tickers)),
        HumanMessage(content=state["question"]),
    ]
    outcome = generate_structured_bounded(
        deps.model, messages, ToolPlan, temperature=0.0, max_tokens=800
    )
    timer.llm_calls = outcome.attempts
    if not outcome.ok:
        return ResearchState(
            planning_method="structured_planner",
            plan=[],
            answer=(
                f"The research assistant is temporarily unavailable ({outcome.error})."
            ),
            llm_calls=state.get("llm_calls", 0) + outcome.attempts,
            node_runs=[timer.done("failed")],
            errors=[
                GraphError(
                    node="plan_research",
                    category=outcome.error_category or "StructuredOutputError",
                    message=redact(outcome.error or ""),
                    recoverable=True,
                    checkpointed=True,
                    occurred_at=datetime.now(UTC).isoformat(),
                )
            ],
        )
    assert outcome.value is not None
    return ResearchState(
        planning_method="structured_planner",
        plan=[
            PlannedCall(tool_name=item.tool_name, arguments=item.arguments)
            for item in outcome.value.items[:MAX_TOOL_CALLS]
        ],
        llm_calls=state.get("llm_calls", 0) + outcome.attempts,
        node_runs=[timer.done("ok")],
    )


# --- Stage 4: tool execution --------------------------------------------


def execute_tools(state: ResearchState, deps: GraphDeps) -> ResearchState:
    timer = _Timer("execute_tools")
    records: list[ToolRecord] = []
    for planned in state.get("plan", []):
        records.append(
            _run_one_tool(
                planned["tool_name"],
                planned["arguments"],
                deps,
                resolved_tickers=state.get("resolved_tickers") or None,
                as_of=_as_of_date(state),
                retrieval_round=0,
            )
        )
    timer.tool_calls = len(records)
    status = "ok"
    if records and all(not r["success"] for r in records):
        status = "degraded"
    return ResearchState(tool_records=records, node_runs=[timer.done(status)])


def _run_one_tool(
    name: str,
    arguments: dict[str, Any],
    deps: GraphDeps,
    *,
    resolved_tickers: list[str] | None,
    as_of: date | None,
    retrieval_round: int,
) -> ToolRecord:
    """One tool call, with the same per-call failure isolation the legacy
    orchestrator has: an exception becomes a failed record, never an
    exception that ends the run."""
    start = time.monotonic()
    tool = deps.registry.get(name)
    category = TOOL_EVIDENCE_CATEGORY.get(name, EvidenceCategory.DERIVED).value
    if tool is None:
        return ToolRecord(
            tool_name=name,
            arguments=arguments,
            success=False,
            duration_ms=(time.monotonic() - start) * 1000,
            summary="",
            error=f"unknown tool {name!r}",
            query_description=None,
            evidence_category=category,
            retrieval_round=retrieval_round,
        )
    resolved_args = apply_deterministic_defaults(tool, arguments, resolved_tickers, as_of)
    try:
        outcome = tool.run(tool.args_schema.model_validate(resolved_args))
    except Exception as exc:  # noqa: BLE001 — a tool failure must degrade, not crash
        return ToolRecord(
            tool_name=name,
            arguments=resolved_args,
            success=False,
            duration_ms=(time.monotonic() - start) * 1000,
            summary="",
            error=redact(str(exc)),
            query_description=None,
            evidence_category=category,
            retrieval_round=retrieval_round,
        )
    deps.outcomes[_outcome_key(name, resolved_args, retrieval_round)] = outcome
    return ToolRecord(
        tool_name=name,
        arguments=resolved_args,
        success=outcome.success,
        duration_ms=(time.monotonic() - start) * 1000,
        summary=outcome.summary,
        error=outcome.error,
        query_description=outcome.query_description,
        evidence_category=category,
        retrieval_round=retrieval_round,
    )


def _outcome_key(name: str, arguments: dict[str, Any], retrieval_round: int) -> str:
    """Identifies one tool call within one run.

    ``GraphDeps.outcomes`` is a per-run, in-memory side table holding the
    full ``ToolOutcome`` objects, because state must stay JSON-safe while
    ``merge_evidence`` still needs real ``Citation`` objects and raw data.
    It is NOT checkpointed: a resumed run rebuilds evidence from the
    checkpointed records and blocks, which is why ``merge_evidence``
    tolerates a missing outcome.
    """
    return f"{retrieval_round}:{name}:{sorted(arguments.items())!r}"


# --- Stage 5: merge evidence -------------------------------------------


def merge_evidence(state: ResearchState, deps: GraphDeps) -> ResearchState:
    """Recomputed from the FULL record list every time, so running it again
    after a targeted retrieval round replaces rather than duplicates."""
    timer = _Timer("merge_evidence")
    blocks: list[EvidenceBlock] = []
    citations: list[CitationRef] = []
    existing_blocks = {
        (b["tool_name"], b["retrieval_round"]): b for b in state.get("evidence_blocks", [])
    }

    for record in state.get("tool_records", []):
        key = _outcome_key(
            record["tool_name"], record.get("arguments") or {}, record["retrieval_round"]
        )
        outcome = deps.outcomes.get(key)
        legacy_record = ToolCallRecord(
            tool_name=record["tool_name"],
            arguments=record.get("arguments") or {},
            success=record["success"],
            duration_ms=record["duration_ms"],
            summary=record["summary"],
            error=record.get("error"),
            query_description=record.get("query_description"),
        )
        if outcome is None and record["success"]:
            # Resumed run: the ToolOutcome objects did not survive the
            # process, but the checkpointed block did. Carry it forward
            # rather than re-running the tool or dropping real evidence.
            carried = existing_blocks.get((record["tool_name"], record["retrieval_round"]))
            if carried is not None:
                blocks.append(carried)
            continue

        # A tool that RAISED has no outcome either, and that is a different
        # thing entirely: the legacy orchestrator emits a "### tool —
        # FAILED" block precisely so synthesis is told which tools failed
        # and can answer honestly around the gap (see the docstring on
        # agents/orchestrator.py). Collapsing the two cases here silently
        # dropped the gap and left synthesis with nothing to say.
        block = EvidenceBlock(
            tool_name=record["tool_name"],
            category=record["evidence_category"],
            text=evidence_block_text(legacy_record, outcome if record["success"] else None),
            item_count=(
                _item_count(record["tool_name"], outcome.data, outcome.citations)
                if outcome is not None and outcome.success
                else 0
            ),
            retrieval_round=record["retrieval_round"],
        )
        if outcome is not None:
            reported = _reported_earnings_date(record["tool_name"], outcome.data)
            if reported:
                block["reported_earnings_date"] = reported
        blocks.append(block)
        if outcome is not None and outcome.success:
            citations.extend(_citation_ref(c) for c in outcome.citations)

    return ResearchState(
        evidence_blocks=blocks,
        citations=citations,
        node_runs=[timer.done("ok")],
    )


def evidence_text(state: ResearchState) -> str:
    """The exact string both runtimes hand to synthesis and verification."""
    return "\n\n".join(b["text"] for b in state.get("evidence_blocks", []))


# --- Stage 6: the evidence-quality gate (deterministic) -----------------


def evidence_quality_gate(state: ResearchState, deps: GraphDeps) -> ResearchState:
    timer = _Timer("evidence_quality_gate", attempt=state.get("retrieval_attempt_count", 0) + 1)
    result = assess_evidence(
        intent=state.get("intent") or IntentCategory.GENERAL.value,
        tool_records=state.get("tool_records", []),
        evidence_blocks=state.get("evidence_blocks", []),
        citations=state.get("citations", []),
        as_of=_as_of_date(state),
        calendar_earnings_date=_calendar_earnings_date(state, deps),
    )
    return ResearchState(
        evidence_quality=result.model_dump(mode="json"),
        node_runs=[timer.done("ok")],
    )


def _calendar_earnings_date(state: ResearchState, deps: GraphDeps) -> date | None:
    event_id = state.get("earnings_calendar_event_id")
    if event_id is None:
        return None
    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415

    event = (
        deps.db.query(EarningsCalendarEvent)
        .filter(EarningsCalendarEvent.id == event_id)
        .one_or_none()
    )
    return event.earnings_date if event is not None else None


# --- Stage 7: targeted retrieval (requirement 29) -----------------------

#: Which tool to re-run for a category that came back missing or thin.
#: Only the gap is retried -- never the whole plan.
_RETRY_TOOL_BY_CATEGORY: dict[str, str] = {
    EvidenceCategory.FILING.value: "search_filings",
    EvidenceCategory.EARNINGS_HISTORY.value: "get_historical_earnings",
    EvidenceCategory.ESTIMATES.value: "get_analyst_estimates",
    EvidenceCategory.GUIDANCE.value: "compare_guidance",
    EvidenceCategory.OPTIONS_CONTEXT.value: "get_options_snapshot",
}


def targeted_retrieve(state: ResearchState, deps: GraphDeps) -> ResearchState:
    """Re-runs ONLY the tools behind the recommended categories.

    Zero LLM calls: which tool serves which category is a fact about this
    codebase, so asking a model to re-plan would spend a billed call to
    rediscover a mapping that is already written down.

    For filing evidence the retry goes through the retriever adapter with a
    widened ``k`` rather than repeating the identical tool call -- a second
    identical search returns the identical nothing.
    """
    round_number = state.get("retrieval_attempt_count", 0) + 1
    timer = _Timer("targeted_retrieve", attempt=round_number)
    quality = state.get("evidence_quality") or {}
    categories: list[str] = list(quality.get("recommended_retrieval") or [])
    records: list[ToolRecord] = []
    tickers = state.get("resolved_tickers") or None
    as_of = _as_of_date(state)

    for category in categories:
        tool_name = _RETRY_TOOL_BY_CATEGORY.get(category)
        if tool_name is None:
            continue
        if tool_name == "search_filings":
            records.append(
                _retry_filing_search(state, deps, round_number=round_number, as_of=as_of)
            )
            continue
        records.append(
            _run_one_tool(
                tool_name,
                {},
                deps,
                resolved_tickers=tickers,
                as_of=as_of,
                retrieval_round=round_number,
            )
        )

    timer.tool_calls = len(records)
    return ResearchState(
        tool_records=records,
        retrieval_attempt_count=round_number,
        node_runs=[timer.done("ok" if records else "skipped")],
    )


#: Round-0 filing search uses the tool's own default k; the retry widens
#: it once. Bounded on purpose -- see Part S, this spends real embedding
#: and database work per round.
RETRY_RETRIEVAL_K = 10


def _targeted_scope_company_ids(
    state: ResearchState, deps: GraphDeps
) -> tuple[list[int] | None, str | None]:
    """Which companies the targeted retry may read filings from.

    Returns ``(company_ids, refusal)``. ``company_ids`` of None means
    genuinely unscoped -- a question that named no company at all. A
    ``refusal`` string means a company WAS named but could not be scoped,
    and the retry must not run: an unscoped hybrid search over the whole
    corpus would answer a question about one issuer with another issuer's
    filings.

    ``company_id`` cannot be the only scope. It is populated only when a
    question resolved to exactly one ticker (see api/routers/research.py),
    so a question naming two companies -- or one reaching the graph without
    that lookup -- previously retried against the entire corpus. Found live
    on 2026-10-01: a guidance question scoped to MU came back citing ACN,
    AFRM, AMD and CASY filings. ``resolved_tickers`` is the authoritative
    scope, is already in state, and is resolved to ids here exactly as the
    ``search_filings`` tool does it.
    """
    company_id = state.get("company_id")
    if company_id is not None:
        return [company_id], None
    tickers = sorted({t.upper() for t in (state.get("resolved_tickers") or []) if t})
    if not tickers:
        return None, None

    from models.company import Company  # noqa: PLC0415

    ids = [row[0] for row in deps.db.query(Company.id).filter(Company.ticker.in_(tickers)).all()]
    if not ids:
        # Fail closed. rag.retrieval treats an empty company_ids list as
        # "no filter" (``if filters.company_ids:``), so passing [] through
        # would silently widen the search to every company rather than
        # narrow it to none.
        return None, f"could not resolve {', '.join(tickers)} to a covered company"
    return ids, None


def _retry_filing_search(
    state: ResearchState, deps: GraphDeps, *, round_number: int, as_of: date | None
) -> ToolRecord:
    start = time.monotonic()
    company_ids, refusal = _targeted_scope_company_ids(state, deps)
    if refusal is not None:
        return ToolRecord(
            tool_name="search_filings",
            arguments={"query": state["question"], "k": RETRY_RETRIEVAL_K},
            success=False,
            duration_ms=(time.monotonic() - start) * 1000,
            summary="",
            error=f"targeted filing retrieval skipped -- {refusal}",
            query_description=None,
            evidence_category=EvidenceCategory.FILING.value,
            retrieval_round=round_number,
        )
    retriever = EDLHybridRetriever(
        db=deps.db,
        embedder=deps.embedder,
        k=RETRY_RETRIEVAL_K,
        scope=RetrievalScope(company_ids=company_ids, as_of=as_of),
    )
    try:
        documents = retriever.invoke(state["question"])
    except Exception as exc:  # noqa: BLE001 — same degrade-not-crash policy
        return ToolRecord(
            tool_name="search_filings",
            arguments={"query": state["question"], "k": RETRY_RETRIEVAL_K},
            success=False,
            duration_ms=(time.monotonic() - start) * 1000,
            summary="",
            error=redact(str(exc)),
            query_description=None,
            evidence_category=EvidenceCategory.FILING.value,
            retrieval_round=round_number,
        )

    from rag.context import assemble_context  # noqa: PLC0415
    from rag.retrieval import RetrievedChunk  # noqa: PLC0415

    chunks = [
        RetrievedChunk(
            chunk_id=d.metadata["chunk_id"],
            filing_id=d.metadata["filing_id"],
            company_id=d.metadata["company_id"],
            ticker=d.metadata["ticker"],
            filing_type=d.metadata["filing_type"],
            filing_date=date.fromisoformat(d.metadata["filing_date"]),
            source_url=d.metadata["source_url"],
            section=d.metadata["section"],
            chunk_index=d.metadata["chunk_index"],
            text=d.page_content,
            score=d.metadata["score"],
            accession_number=d.metadata.get("accession_number"),
        )
        for d in documents
    ]
    assembled = assemble_context(chunks, evidence_cutoff=as_of)
    from agents.tools.types import ToolOutcome  # noqa: PLC0415

    outcome = ToolOutcome(
        success=True,
        summary=(
            f"Retrieved {len(chunks)} filing excerpts on a targeted second pass "
            f"(k={RETRY_RETRIEVAL_K})."
        ),
        data={"context_text": assembled.context_text},
        citations=assembled.citations,
    )
    arguments = {"query": state["question"], "k": RETRY_RETRIEVAL_K}
    deps.outcomes[_outcome_key("search_filings", arguments, round_number)] = outcome
    return ToolRecord(
        tool_name="search_filings",
        arguments=arguments,
        success=True,
        duration_ms=(time.monotonic() - start) * 1000,
        summary=outcome.summary,
        error=None,
        query_description=None,
        evidence_category=EvidenceCategory.FILING.value,
        retrieval_round=round_number,
    )


# --- Stage 8: synthesis -------------------------------------------------


def synthesize(state: ResearchState, deps: GraphDeps) -> ResearchState:
    timer = _Timer("synthesize")
    text = evidence_text(state)
    if not text.strip():
        # No evidence: the legacy orchestrator returns an empty draft here
        # rather than inventing one, and so does this.
        return ResearchState(node_runs=[timer.done("skipped")])
    messages = [
        ChatMessage(role="system", content=SYNTHESIS_SYSTEM_PROMPT),
        ChatMessage(
            role="user", content=build_synthesis_user_prompt(state["question"], text)
        ),
    ]
    try:
        result = deps.model.provider.generate(messages, temperature=0.0, max_tokens=900)
    except LLMError as exc:
        timer.llm_calls = 1
        return ResearchState(
            answer=f"The research assistant could not complete this answer ({exc}).",
            llm_calls=state.get("llm_calls", 0) + 1,
            node_runs=[timer.done("failed")],
            errors=[_error("synthesize", exc, recoverable=True)],
        )
    timer.llm_calls = 1
    usage = result.usage
    return ResearchState(
        answer=result.content or "",
        llm_calls=state.get("llm_calls", 0) + 1,
        input_tokens=state.get("input_tokens", 0) + (usage.input_tokens if usage else 0),
        output_tokens=state.get("output_tokens", 0) + (usage.output_tokens if usage else 0),
        node_runs=[timer.done("ok")],
    )


# --- Stage 9: verification (best-effort) --------------------------------


def verify(state: ResearchState, deps: GraphDeps) -> ResearchState:
    timer = _Timer("verify", attempt=state.get("revision_count", 0) + 1)
    text = evidence_text(state)
    if not text.strip() or not state.get("answer", "").strip():
        return ResearchState(node_runs=[timer.done("skipped")])
    messages = [
        SystemMessage(content=VERIFICATION_SYSTEM_PROMPT),
        HumanMessage(content=build_verification_user_prompt(text, state["answer"])),
    ]
    outcome = generate_structured_bounded(
        deps.model, messages, VerificationResult, temperature=0.0, max_tokens=500
    )
    timer.llm_calls = outcome.attempts
    if not outcome.ok:
        # Best-effort, exactly as before: an unavailable verifier leaves the
        # draft standing and says so, rather than failing the run.
        #
        # ``verification=None`` CLEARS any earlier verdict rather than
        # leaving it standing (live defect, 2026-10-01). On the revise path
        # this node runs twice: a first verdict of "unsupported" is what
        # triggered the revision, so leaving it in place reported that
        # verdict -- and its ``unsupported_claims`` -- against the REVISED
        # answer, describing claims the revision had already removed. That
        # is the same misattribution this runtime was built to stop legacy
        # doing (see DECLARED_DIFFERENCES in evaluation/agent_parity.py).
        # Cleared, the result reads verification_ran=False, which is the
        # truth: the answer being returned was not checked. Routing is
        # unaffected -- route_after_verify sends a None verdict to END.
        return ResearchState(
            verification=None,
            llm_calls=state.get("llm_calls", 0) + outcome.attempts,
            node_runs=[timer.done("degraded")],
            warnings=[
                "Verification was unavailable, so the answer returned was not checked."
            ],
            errors=[
                GraphError(
                    node="verify",
                    category=outcome.error_category or "StructuredOutputError",
                    message=redact(outcome.error or ""),
                    recoverable=True,
                    checkpointed=True,
                    occurred_at=datetime.now(UTC).isoformat(),
                )
            ],
        )
    assert outcome.value is not None
    return ResearchState(
        verification=outcome.value.model_dump(mode="json"),
        llm_calls=state.get("llm_calls", 0) + outcome.attempts,
        node_runs=[timer.done("ok")],
    )


# --- Stage 10: bounded revision ----------------------------------------


def revise(state: ResearchState, deps: GraphDeps) -> ResearchState:
    timer = _Timer("revise", attempt=state.get("revision_count", 0) + 1)
    text = evidence_text(state)
    verification = state.get("verification") or {}
    unsupported = verification.get("unsupported_claims") or []
    messages = [
        ChatMessage(role="system", content=SYNTHESIS_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=(
                build_synthesis_user_prompt(state["question"], text)
                + "\n\nYour previous draft contained claims not supported by the "
                f"evidence: {unsupported}. Revise the answer to "
                "remove or correct them, using only the evidence above."
            ),
        ),
    ]
    try:
        result = deps.model.provider.generate(messages, temperature=0.0, max_tokens=900)
    except LLMError as exc:
        timer.llm_calls = 1
        return ResearchState(
            revision_count=state.get("revision_count", 0) + 1,
            llm_calls=state.get("llm_calls", 0) + 1,
            node_runs=[timer.done("failed")],
            errors=[_error("revise", exc, recoverable=False)],
        )
    timer.llm_calls = 1
    usage = result.usage
    revised_text = result.content or ""
    return ResearchState(
        answer=revised_text or state.get("answer", ""),
        revised=bool(revised_text),
        revision_count=state.get("revision_count", 0) + 1,
        llm_calls=state.get("llm_calls", 0) + 1,
        input_tokens=state.get("input_tokens", 0) + (usage.input_tokens if usage else 0),
        output_tokens=state.get("output_tokens", 0) + (usage.output_tokens if usage else 0),
        node_runs=[timer.done("ok")],
    )


def new_run_id() -> str:
    return str(uuid.uuid4())
