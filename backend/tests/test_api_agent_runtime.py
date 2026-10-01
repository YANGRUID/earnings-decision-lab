"""The research API's agent-runtime surface (requirements 49-51, 43, 63-65).

Two things matter here: the flag genuinely selects the runtime, and the
response carries honest provenance either way -- including the ABSENCE of
an evidence-quality verdict when the legacy runtime answered, since legacy
has no gate and reporting one would be a fabrication.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from _agent_fakes import _ScriptedLLM, _StubEmbedder
from fastapi.testclient import TestClient

from agents.graph.state import (
    AGENT_RUNTIME_LANGGRAPH,
    AGENT_RUNTIME_LEGACY,
    MAX_RETRIEVAL_ROUNDS,
    MAX_REVISIONS,
)
from models.company import Company
from models.document_chunk import EMBEDDING_DIM, DocumentChunk
from models.enums import FilingType
from models.filing import Filing
from models.research_preparation_job import JobStatus, ResearchPreparationJob
from schemas.agent import (
    IntentCategory,
    IntentClassification,
    ToolPlan,
    ToolPlanItem,
    VerificationResult,
)
from services.llm.types import GenerateResult, TokenUsage

NOW = datetime.now(UTC)


@pytest.fixture(scope="module")
def test_client() -> Iterator[TestClient]:
    from api.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def scripted_llm() -> _ScriptedLLM:
    return _ScriptedLLM(
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
            GenerateResult(
                content="Gross margin expanded [1].",
                usage=TokenUsage(input_tokens=100, output_tokens=30),
            )
        ],
    )


@pytest.fixture
def client(test_client, db_session, scripted_llm) -> Iterator[TestClient]:
    from api.deps import get_db, get_embedder, get_llm

    app = test_client.app
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_llm] = lambda: scripted_llm
    app.dependency_overrides[get_embedder] = lambda: _StubEmbedder()
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def prepared_company(db_session) -> Company:
    """A company whose research is already READY, so /research/query
    actually reaches an orchestrator instead of returning "preparing"."""
    company = Company(ticker="ZZAPI01", name="ZZ API Inc", cik="0009994001")
    db_session.add(company)
    db_session.flush()
    filing = Filing(
        company_id=company.id,
        filing_type=FilingType.FORM_10Q,
        filing_date=date(2026, 2, 18),
        accession_number="ZZAPI01-Q4",
        source_url="https://example.com/zzapi01.htm",
        retrieved_at=NOW,
    )
    db_session.add(filing)
    db_session.flush()
    for index in range(4):
        db_session.add(
            DocumentChunk(
                filing_id=filing.id,
                company_id=company.id,
                chunk_index=index,
                section="Item 7",
                text=f"Gross margin commentary {index}",
                token_count=8,
                embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
                embedding_model="stub",
                retrieved_at=NOW,
            )
        )
    db_session.add(
        ResearchPreparationJob(
            ticker=company.ticker,
            company_id=company.id,
            status=JobStatus.COMPLETED,
            steps=[],
            started_at=NOW,
            completed_at=NOW,
        )
    )
    db_session.flush()
    return company


def _ask(client, ticker: str):
    return client.post(
        "/api/v1/research/query",
        json={"question": "what happened to gross margin?", "ticker": ticker},
    )


# --- the status endpoint ------------------------------------------------


def test_the_status_endpoint_reports_the_declared_shape_and_the_enforced_bounds(client):
    response = client.get("/api/v1/research/agent-runtime")
    assert response.status_code == 200
    body = response.json()

    assert body["configured_runtime"] == "legacy"
    assert body["runtime_version"] == AGENT_RUNTIME_LEGACY
    assert body["max_retrieval_rounds"] == MAX_RETRIEVAL_ROUNDS
    assert body["max_revisions"] == MAX_REVISIONS
    assert body["nodes"][0] == "classify_intent"
    assert "targeted_retrieve" in body["conditional_routes"]["evidence_quality_gate"]
    assert body["warning"] is None


def test_the_status_endpoint_classifies_every_tool(client):
    body = client.get("/api/v1/research/agent-runtime").json()
    assert len(body["tools"]) == 7
    for tool in body["tools"]:
        assert tool["access"] in ("read_only", "derived_calculation")
        assert tool["evidence_category"]


def test_the_status_endpoint_never_echoes_a_dsn(client):
    body = client.get("/api/v1/research/agent-runtime").json()
    assert body["checkpoint_schema"] == "langgraph"
    assert "change_me" not in (body.get("checkpoint_detail") or "")


# --- the flag genuinely selects the runtime ----------------------------


def test_the_default_runtime_is_legacy_and_reports_no_workflow(
    client, db_session, prepared_company
):
    response = _ask(client, prepared_company.ticker)
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "completed"
    assert body["answer"] == "Gross margin expanded [1]."
    assert body["agent_runtime"] == AGENT_RUNTIME_LEGACY
    # Legacy has no evidence-quality gate, so the field is ABSENT rather
    # than defaulted to a verdict it never reached.
    assert body["workflow"] is None


def test_setting_the_flag_routes_the_question_through_the_graph(
    client, db_session, prepared_company, monkeypatch
):
    from core.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_RUNTIME", "langgraph")
    assert Settings().agent_runtime == "langgraph"

    try:
        response = _ask(client, prepared_company.ticker)
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    body = response.json()
    assert body["agent_runtime"] == AGENT_RUNTIME_LANGGRAPH
    workflow = body["workflow"]
    assert workflow is not None
    assert workflow["graph_version"] == "edl-research-graph-v1"
    assert workflow["run_id"]
    assert workflow["evidence_quality"]["status"] in ("sufficient", "partial", "insufficient")
    assert [n["node"] for n in workflow["node_runs"]][0] == "classify_intent"
    # Requirement 42: the operational trace carries timings and counts,
    # never a prompt or a model output.
    for node in workflow["node_runs"]:
        assert set(node) == {
            "node",
            "started_at",
            "finished_at",
            "duration_ms",
            "status",
            "llm_calls",
            "tool_calls",
            "attempt",
        }


def test_an_unknown_runtime_answers_with_legacy_and_warns(client, monkeypatch):
    from core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_RUNTIME", "langraph")
    try:
        body = client.get("/api/v1/research/agent-runtime").json()
    finally:
        get_settings.cache_clear()

    assert body["configured_runtime"] == "legacy"
    assert "not a known runtime" in body["warning"]


def test_checkpointing_is_off_by_default(client):
    body = client.get("/api/v1/research/agent-runtime").json()
    assert body["checkpointing_enabled"] is False
