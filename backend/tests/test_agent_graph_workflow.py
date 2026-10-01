"""End-to-end LangGraph runs: the bounded loops, the targeted retrieval
round, and honest failure recording (requirements 70-72, 75, 76, 33, 41).

These drive the real graph against the real tools and the test database,
with only the LLM scripted -- so a "the gate asked again and got evidence"
assertion is a statement about real retrieval, not about a mock.
"""

from datetime import UTC, date, datetime

from _agent_fakes import _ScriptedLLM, _StubEmbedder

from agents.graph.runtime import run_research_graph
from agents.graph.state import MAX_RETRIEVAL_ROUNDS, MAX_REVISIONS
from models.company import Company
from models.document_chunk import EMBEDDING_DIM, DocumentChunk
from models.enums import FilingType
from models.filing import Filing
from schemas.agent import (
    EvidenceQualityStatus,
    IntentCategory,
    IntentClassification,
    ToolPlan,
    ToolPlanItem,
    VerificationResult,
)
from services.llm.errors import LLMRequestError, StructuredOutputError
from services.llm.types import GenerateResult, TokenUsage

NOW = datetime.now(UTC)


def _seed_filing(db, ticker: str, cik: str, text: str, *, chunks: int = 1) -> Company:
    company = Company(ticker=ticker, name=f"{ticker} Inc", cik=cik)
    db.add(company)
    db.flush()
    filing = Filing(
        company_id=company.id,
        filing_type=FilingType.FORM_10Q,
        filing_date=date(2026, 2, 18),
        accession_number=f"TEST-{ticker}-01",
        source_url=f"https://example.com/{ticker}.htm",
        retrieved_at=NOW,
    )
    db.add(filing)
    db.flush()
    for index in range(chunks):
        db.add(
            DocumentChunk(
                filing_id=filing.id,
                company_id=company.id,
                chunk_index=index,
                section="Item 7",
                text=f"{text} (part {index})",
                token_count=8,
                embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
                embedding_model="stub",
                retrieved_at=NOW,
            )
        )
    db.flush()
    return company


def _llm(*, intent, plan=None, generate=None, verification=None, supports_tools=False):
    structured: dict = {
        IntentClassification: [IntentClassification(category=intent, reasoning="r")]
    }
    if plan is not None:
        structured[ToolPlan] = [plan]
    if verification is not None:
        structured[VerificationResult] = verification
    return _ScriptedLLM(
        supports_tool_calling=supports_tools,
        structured_responses=structured,
        generate_responses=generate or [],
    )


# --- a complete, sufficient run ----------------------------------------


def test_a_sufficient_run_never_enters_either_loop(db_session):
    company = _seed_filing(
        db_session, "ZZLG01", "0009991001", "Gross margin expanded on favourable mix", chunks=4
    )
    llm = _llm(
        intent=IntentCategory.FILING_RESEARCH,
        plan=ToolPlan(
            items=[ToolPlanItem(tool_name="search_filings", arguments={"query": "margin"})]
        ),
        generate=[
            GenerateResult(
                content="Margins expanded [1].",
                usage=TokenUsage(input_tokens=90, output_tokens=25),
            )
        ],
        verification=[VerificationResult(supported=True)],
    )

    result = run_research_graph(
        db_session, llm, _StubEmbedder(), "what happened to margins?",
        resolved_tickers=[company.ticker], company_id=company.id,
    )

    assert result.response.answer == "Margins expanded [1]."
    assert result.evidence_quality is not None
    assert result.evidence_quality["status"] == EvidenceQualityStatus.SUFFICIENT.value
    assert result.retrieval_rounds == 0
    assert result.revision_count == 0
    assert result.response.citations, "filing evidence must carry citations through the graph"
    assert [n["node"] for n in result.node_runs] == [
        "classify_intent",
        "window_context",
        "plan_research",
        "execute_tools",
        "merge_evidence",
        "evidence_quality_gate",
        "synthesize",
        "verify",
    ]


# --- requirement 70: insufficient -> targeted retry --------------------


def test_thin_evidence_triggers_exactly_one_targeted_retrieval_round(db_session):
    """Round 0 asks for a single chunk; the gate finds no citation-backed
    filing evidence worth the intent and asks again through the retriever
    with a wider k. The retry must re-run ONLY the filing search."""
    company = _seed_filing(
        db_session, "ZZLG02", "0009991002", "Capex guidance was raised", chunks=6
    )
    llm = _llm(
        intent=IntentCategory.FILING_RESEARCH,
        plan=ToolPlan(
            items=[
                ToolPlanItem(
                    tool_name="get_historical_earnings", arguments={"ticker": company.ticker}
                )
            ]
        ),
        generate=[GenerateResult(content="Answer from retried evidence [1].")],
        verification=[VerificationResult(supported=True)],
    )

    result = run_research_graph(
        db_session, llm, _StubEmbedder(), "what did the filing say about capex?",
        resolved_tickers=[company.ticker], company_id=company.id,
    )

    assert result.retrieval_rounds == 1
    called = {tc.tool_name for tc in result.response.trace.tool_calls}
    assert "search_filings" in called, "the gap category was retried"
    retried = [n for n in result.node_runs if n["node"] == "targeted_retrieve"]
    assert len(retried) == 1
    assert retried[0]["tool_calls"] == 1, "only the missing category was retried, not the plan"
    assert result.response.citations, "the retry produced real citations"


def test_the_retrieval_loop_cannot_run_twice(db_session):
    """Nothing in this fixture can satisfy the gate, so the bound is the
    only thing that stops it (requirement 71)."""
    company = Company(ticker="ZZLG03", name="ZZLG03 Inc", cik="0009991003")
    db_session.add(company)
    db_session.flush()
    llm = _llm(
        intent=IntentCategory.GUIDANCE_COMPARISON,
        plan=ToolPlan(
            items=[ToolPlanItem(tool_name="compare_guidance", arguments={"ticker": "ZZLG03"})]
        ),
        generate=[GenerateResult(content="No guidance is on record for ZZLG03.")],
        verification=[VerificationResult(supported=True)],
    )

    result = run_research_graph(
        db_session, llm, _StubEmbedder(), "how did guidance change?",
        resolved_tickers=["ZZLG03"], company_id=company.id,
    )

    assert result.retrieval_rounds == MAX_RETRIEVAL_ROUNDS == 1
    gate_runs = [n for n in result.node_runs if n["node"] == "evidence_quality_gate"]
    assert len(gate_runs) == 2, "the gate ran once per round and then stopped"
    assert result.evidence_quality is not None
    assert result.evidence_quality["status"] != EvidenceQualityStatus.SUFFICIENT.value
    # And it still answered honestly rather than raising.
    assert "No guidance is on record" in result.response.answer


# --- requirement 72: bounded revision ----------------------------------


def test_an_unsupported_answer_is_revised_exactly_once(db_session):
    company = _seed_filing(db_session, "ZZLG04", "0009991004", "Inventory fell", chunks=4)
    llm = _llm(
        intent=IntentCategory.FILING_RESEARCH,
        plan=ToolPlan(
            items=[ToolPlanItem(tool_name="search_filings", arguments={"query": "inventory"})]
        ),
        generate=[
            GenerateResult(content="Inventory fell 40% and the CEO resigned."),
            GenerateResult(content="Inventory fell [1]."),
        ],
        verification=[
            VerificationResult(supported=False, unsupported_claims=["the CEO resigned"]),
            VerificationResult(supported=False, unsupported_claims=["still unsupported"]),
        ],
    )

    result = run_research_graph(
        db_session, llm, _StubEmbedder(), "what happened to inventory?",
        resolved_tickers=[company.ticker], company_id=company.id,
    )

    assert result.revision_count == MAX_REVISIONS == 1
    assert result.response.answer == "Inventory fell [1]."
    assert result.response.trace.revised is True
    assert [n["node"] for n in result.node_runs].count("verify") == 2
    assert [n["node"] for n in result.node_runs].count("revise") == 1
    # The second verification still said unsupported; the bound, not the
    # model, is what ended the loop.
    assert result.response.trace.verification_supported is False


# --- requirement 75: honest failure recording --------------------------


def test_a_provider_timeout_is_recorded_with_its_real_category(db_session):
    company = _seed_filing(db_session, "ZZLG05", "0009991005", "Revenue grew", chunks=4)
    llm = _llm(
        intent=IntentCategory.FILING_RESEARCH,
        plan=ToolPlan(
            items=[ToolPlanItem(tool_name="search_filings", arguments={"query": "revenue"})]
        ),
        generate=[LLMRequestError("deepseek request timed out after 60s")],
        verification=[VerificationResult(supported=True)],
    )

    result = run_research_graph(
        db_session, llm, _StubEmbedder(), "how did revenue do?",
        resolved_tickers=[company.ticker], company_id=company.id,
    )

    assert "could not complete this answer" in result.response.answer
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error["node"] == "synthesize"
    assert error["category"] == "LLMRequestError", "not collapsed into a generic AgentError"
    assert "timed out" in error["message"]
    assert error["recoverable"] is True
    synth = [n for n in result.node_runs if n["node"] == "synthesize"][0]
    assert synth["status"] == "failed"


def test_an_unclassifiable_question_degrades_instead_of_failing(db_session):
    company = _seed_filing(db_session, "ZZLG06", "0009991006", "Backlog rose", chunks=4)
    llm = _ScriptedLLM(
        structured_responses={
            IntentClassification: [
                StructuredOutputError("intent json malformed"),
                StructuredOutputError("intent json malformed"),
            ],
            ToolPlan: [
                ToolPlan(
                    items=[
                        ToolPlanItem(tool_name="search_filings", arguments={"query": "backlog"})
                    ]
                )
            ],
            VerificationResult: [VerificationResult(supported=True)],
        },
        generate_responses=[GenerateResult(content="Backlog rose [1].")],
    )

    result = run_research_graph(
        db_session, llm, _StubEmbedder(), "tell me about the backlog",
        resolved_tickers=[company.ticker], company_id=company.id,
    )

    assert result.response.trace.intent_category == IntentCategory.GENERAL.value
    assert result.response.answer == "Backlog rose [1]."
    classify = [n for n in result.node_runs if n["node"] == "classify_intent"][0]
    assert classify["status"] == "degraded"
    assert any("handled as a general research question" in w for w in result.warnings)


def test_a_failing_tool_does_not_end_the_run(db_session, monkeypatch):
    company = _seed_filing(db_session, "ZZLG07", "0009991007", "Churn fell", chunks=4)

    def _boom(self, args):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr("agents.tools.earnings_history.EarningsHistoryTool.run", _boom)
    llm = _llm(
        intent=IntentCategory.EARNINGS_HISTORY,
        plan=ToolPlan(
            items=[
                ToolPlanItem(
                    tool_name="get_historical_earnings", arguments={"ticker": company.ticker}
                )
            ]
        ),
        generate=[GenerateResult(content="No earnings history is available.")],
        verification=[VerificationResult(supported=True)],
    )

    result = run_research_graph(
        db_session, llm, _StubEmbedder(), "how has it reacted to earnings?",
        resolved_tickers=[company.ticker], company_id=company.id,
    )

    failed = [tc for tc in result.response.trace.tool_calls if not tc.success]
    assert failed, "the failure is recorded, not swallowed"
    assert "relation does not exist" in (failed[0].error or "")
    assert result.response.answer, "the run still produced an honest answer"


# --- requirement 76: quota exhaustion does not loop --------------------


def test_quota_exhaustion_does_not_loop(db_session):
    """A quota error is returned once per node that hits it -- never
    retried in place, and never used to re-enter a loop."""
    from services.llm.errors import LLMRequestError as QuotaError

    company = _seed_filing(db_session, "ZZLG08", "0009991008", "ARR grew", chunks=4)
    llm = _ScriptedLLM(
        # No native tool calling, so BOTH planning and classification go
        # through the structured path and the call count is the whole
        # story rather than half of it.
        supports_tool_calling=False,
        structured_responses={
            IntentClassification: [QuotaError("429 insufficient_quota")],
            ToolPlan: [QuotaError("429 insufficient_quota")],
        },
        generate_responses=[],
    )

    result = run_research_graph(
        db_session, llm, _StubEmbedder(), "how is ARR trending?",
        resolved_tickers=[company.ticker], company_id=company.id,
    )

    # Two nodes each hit quota once. Neither retried: the structured
    # adapter returns immediately on a non-parse LLMError.
    assert len(llm.generate_structured_calls) == 2
    assert result.llm_calls == 2
    assert [e["category"] for e in result.errors] == ["LLMRequestError", "LLMRequestError"]
    assert "temporarily unavailable" in result.response.answer
    # No evidence, so no synthesis, no verification, no revision.
    assert result.retrieval_rounds == 0
    assert result.revision_count == 0
    assert result.response.trace.verification_ran is False
