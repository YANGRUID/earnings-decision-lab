"""Checkpoint and resume (requirements 73, 74, 35-38, 88).

The claim being tested is specific: after a mid-graph failure, a resume
continues from the failed node and does NOT re-execute the nodes that
already succeeded. That is the whole reason this project adopted LangGraph,
so it is asserted by counting real provider and tool calls rather than by
trusting that the framework does what it says.
"""

from datetime import UTC, date, datetime

import pytest
from _agent_fakes import _ScriptedLLM, _StubEmbedder
from langgraph.checkpoint.memory import InMemorySaver

from agents.adapters.model import EDLChatModel
from agents.adapters.tools import build_langchain_tools
from agents.graph.checkpoint import (
    CHECKPOINT_TABLES,
    LANGGRAPH_SCHEMA,
    checkpoint_status,
    libpq_url,
    postgres_checkpointer,
)
from agents.graph.deps import GraphDeps
from agents.graph.nodes import merge_evidence
from agents.graph.runtime import (
    checkpoint_thread_id,
    resume_research_graph,
    run_research_graph,
)
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
from services.llm.types import GenerateResult

NOW = datetime.now(UTC)
TEST_DB_URL = "postgresql+psycopg://postgres:change_me@localhost:5434/earnings_decision_lab"


def _seed(db, ticker: str, cik: str) -> Company:
    company = Company(ticker=ticker, name=f"{ticker} Inc", cik=cik)
    db.add(company)
    db.flush()
    filing = Filing(
        company_id=company.id,
        filing_type=FilingType.FORM_10Q,
        filing_date=date(2026, 2, 18),
        accession_number=f"TEST-{ticker}-CP",
        source_url=f"https://example.com/{ticker}.htm",
        retrieved_at=NOW,
    )
    db.add(filing)
    db.flush()
    for index in range(4):
        db.add(
            DocumentChunk(
                filing_id=filing.id,
                company_id=company.id,
                chunk_index=index,
                section="Item 7",
                text=f"Operating margin commentary part {index}",
                token_count=8,
                embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
                embedding_model="stub",
                retrieved_at=NOW,
            )
        )
    db.flush()
    return company


# --- requirement 36: checkpoint identity -------------------------------


def test_an_interactive_run_is_identified_by_its_own_run_id():
    assert checkpoint_thread_id(run_id="abc-123") == "research:abc-123"


def test_a_preparation_run_has_a_deterministic_identity():
    """The same event prepared at the same cutoff must resume, not restart
    (requirement 35)."""
    kwargs = {"company_id": 7, "earnings_calendar_event_id": 42, "as_of": date(2026, 9, 1)}
    first = checkpoint_thread_id(**kwargs)
    second = checkpoint_thread_id(**kwargs)
    assert first == second == "prep:7:42:2026-09-01"


def test_the_same_event_at_a_different_cutoff_is_a_different_thread():
    """Sharing a thread would let one run overwrite the other's state."""
    assert checkpoint_thread_id(
        company_id=7, earnings_calendar_event_id=42, as_of=date(2026, 9, 1)
    ) != checkpoint_thread_id(
        company_id=7, earnings_calendar_event_id=42, as_of=date(2026, 9, 2)
    )


def test_the_two_namespaces_cannot_collide():
    interactive = checkpoint_thread_id(run_id="7:42:2026-09-01")
    prep = checkpoint_thread_id(company_id=7, earnings_calendar_event_id=42, as_of=date(2026, 9, 1))
    assert interactive != prep
    assert interactive.startswith("research:") and prep.startswith("prep:")


def test_a_thread_needs_either_a_run_id_or_an_event():
    with pytest.raises(ValueError, match="run_id or an earnings event"):
        checkpoint_thread_id()


# --- requirements 73, 74: resume without redoing finished work ---------


def test_a_resume_continues_from_the_failed_node_only(db_session):
    company = _seed(db_session, "ZZCP01", "0009992001")
    saver = InMemorySaver()
    llm = _ScriptedLLM(
        supports_tool_calling=False,
        structured_responses={
            IntentClassification: [
                IntentClassification(category=IntentCategory.FILING_RESEARCH, reasoning="r")
            ],
            ToolPlan: [
                ToolPlan(
                    items=[
                        ToolPlanItem(tool_name="search_filings", arguments={"query": "margin"})
                    ]
                )
            ],
            VerificationResult: [VerificationResult(supported=True)],
        },
        # A non-LLMError escapes synthesize's own handler, so the run
        # genuinely stops mid-graph with a checkpoint behind it.
        generate_responses=[
            RuntimeError("embedding sidecar crashed"),
            GenerateResult(content="Operating margin held steady [1]."),
        ],
    )
    run_id = "cp-resume-1"

    with pytest.raises(RuntimeError, match="embedding sidecar crashed"):
        run_research_graph(
            db_session, llm, _StubEmbedder(), "what happened to operating margin?",
            resolved_tickers=[company.ticker], company_id=company.id,
            checkpointer=saver, run_id=run_id,
        )

    calls_before = {
        "structured": len(llm.generate_structured_calls),
        "generate": len(llm.generate_calls),
    }
    assert calls_before["structured"] == 2, "intent + plan ran once each"

    resumed = resume_research_graph(
        db_session, llm, _StubEmbedder(),
        checkpointer=saver, thread_id=checkpoint_thread_id(run_id=run_id),
    )

    assert resumed.response.answer == "Operating margin held steady [1]."
    # Requirement 74: classification and planning were NOT re-run. Their
    # structured-call count is unchanged; only verification was added.
    structured_schemas = [schema for _messages, schema in llm.generate_structured_calls]
    assert structured_schemas.count(IntentClassification) == 1
    assert structured_schemas.count(ToolPlan) == 1
    assert structured_schemas.count(VerificationResult) == 1
    # And the tool was not re-executed. node_runs is itself checkpointed,
    # so the completed nodes are still IN the trace -- exactly once each,
    # which is the statement worth making: they ran before the failure and
    # the resume did not repeat them.
    nodes = [n["node"] for n in resumed.node_runs]
    assert nodes.count("classify_intent") == 1
    assert nodes.count("plan_research") == 1
    assert nodes.count("execute_tools") == 1
    assert len(resumed.response.trace.tool_calls) == 1, "no second filing search"
    # synthesize is the node that failed, so it is the one that ran twice.
    assert nodes.count("synthesize") == 1, "only the successful attempt is recorded"


def test_the_resumed_run_keeps_the_evidence_it_had_already_collected(db_session):
    company = _seed(db_session, "ZZCP02", "0009992002")
    saver = InMemorySaver()
    llm = _ScriptedLLM(
        supports_tool_calling=False,
        structured_responses={
            IntentClassification: [
                IntentClassification(category=IntentCategory.FILING_RESEARCH, reasoning="r")
            ],
            ToolPlan: [
                ToolPlan(
                    items=[
                        ToolPlanItem(tool_name="search_filings", arguments={"query": "margin"})
                    ]
                )
            ],
            VerificationResult: [VerificationResult(supported=True)],
        },
        generate_responses=[RuntimeError("transient"), GenerateResult(content="Answer [1].")],
    )
    run_id = "cp-resume-2"
    with pytest.raises(RuntimeError):
        run_research_graph(
            db_session, llm, _StubEmbedder(), "margins?",
            resolved_tickers=[company.ticker], company_id=company.id,
            checkpointer=saver, run_id=run_id,
        )

    resumed = resume_research_graph(
        db_session, llm, _StubEmbedder(),
        checkpointer=saver, thread_id=checkpoint_thread_id(run_id=run_id),
    )

    assert resumed.response.citations, "citations survived the checkpoint round trip"
    assert resumed.evidence_quality is not None
    assert resumed.response.trace.tool_calls, "the tool record survived too"


def test_merge_evidence_carries_a_checkpointed_block_when_the_outcome_is_gone(db_session):
    """The resume path in isolation: a NEW process has no ToolOutcome
    objects, only the checkpointed block. It must be carried forward, not
    dropped and not re-fetched."""
    _, registry = build_langchain_tools(db_session, _StubEmbedder())
    deps = GraphDeps(
        db=db_session,
        model=EDLChatModel(provider=_ScriptedLLM()),
        embedder=_StubEmbedder(),
        registry=registry,
        outcomes={},  # a fresh process
    )
    state = {
        "tool_records": [
            {
                "tool_name": "search_filings",
                "arguments": {"query": "margin"},
                "success": True,
                "duration_ms": 5.0,
                "summary": "Retrieved 3 filing excerpts",
                "error": None,
                "query_description": None,
                "evidence_category": "filing",
                "retrieval_round": 0,
            }
        ],
        "evidence_blocks": [
            {
                "tool_name": "search_filings",
                "category": "filing",
                "text": "### search_filings\nRetrieved 3 filing excerpts\n...",
                "item_count": 3,
                "retrieval_round": 0,
            }
        ],
    }

    merged = merge_evidence(state, deps)  # type: ignore[arg-type]

    assert len(merged["evidence_blocks"]) == 1
    assert merged["evidence_blocks"][0]["item_count"] == 3


# --- requirements 38, 88: checkpoints are not financial evidence --------


def test_the_checkpointer_tables_live_outside_the_domain_schema():
    import psycopg

    with postgres_checkpointer(TEST_DB_URL):
        pass
    with psycopg.connect(libpq_url(TEST_DB_URL), autocommit=True) as conn:
        rows = conn.execute(
            "select schemaname, tablename from pg_tables where tablename = any(%s)",
            (list(CHECKPOINT_TABLES),),
        ).fetchall()
    assert rows, "the checkpointer must have created its tables"
    for schema, table in rows:
        assert schema == LANGGRAPH_SCHEMA, f"{table} landed in {schema}, not {LANGGRAPH_SCHEMA}"


def test_checkpoint_status_reports_readiness_without_exposing_a_dsn():
    status = checkpoint_status(TEST_DB_URL)
    assert status.schema == LANGGRAPH_SCHEMA
    assert "change_me" not in (status.reason or ""), "a status must never echo credentials"


def test_checkpoint_status_never_raises_on_an_unreachable_database():
    status = checkpoint_status(
        "postgresql+psycopg://postgres:change_me@localhost:1/nonexistent_db"
    )
    assert status.available is False
    assert status.reason
    assert "change_me" not in status.reason


def test_a_real_postgres_checkpointed_run_resumes(db_session):
    """The same resume behaviour, through the production checkpointer
    rather than the in-memory one (requirement 37)."""
    company = _seed(db_session, "ZZCP03", "0009992003")
    llm = _ScriptedLLM(
        supports_tool_calling=False,
        structured_responses={
            IntentClassification: [
                IntentClassification(category=IntentCategory.FILING_RESEARCH, reasoning="r")
            ],
            ToolPlan: [
                ToolPlan(
                    items=[
                        ToolPlanItem(tool_name="search_filings", arguments={"query": "margin"})
                    ]
                )
            ],
            VerificationResult: [VerificationResult(supported=True)],
        },
        generate_responses=[
            RuntimeError("provider socket closed"),
            GenerateResult(content="Margin commentary summarised [1]."),
        ],
    )
    run_id = f"cp-pg-{NOW.timestamp()}"

    with postgres_checkpointer(TEST_DB_URL) as saver:
        with pytest.raises(RuntimeError, match="provider socket closed"):
            run_research_graph(
                db_session, llm, _StubEmbedder(), "margins?",
                resolved_tickers=[company.ticker], company_id=company.id,
                checkpointer=saver, run_id=run_id,
            )
        resumed = resume_research_graph(
            db_session, llm, _StubEmbedder(),
            checkpointer=saver, thread_id=checkpoint_thread_id(run_id=run_id),
        )

    assert resumed.response.answer == "Margin commentary summarised [1]."
    schemas = [schema for _m, schema in llm.generate_structured_calls]
    assert schemas.count(IntentClassification) == 1, "classification was not repeated"
