"""Legacy vs LangGraph structural parity over a small corpus
(requirements 77, 44-48, 82-84).

The corpus is the one from Part V, mapped onto this project's real tools:
an earnings-history question, a filing-risk question, a guidance
comparison, an options-analytics question and a general one. Each runs
both runtimes against the same seeded rows and the same script.

Prose is never compared. Call budgets are.
"""

from datetime import UTC, date, datetime

import pytest
from _agent_fakes import _ScriptedLLM, _StubEmbedder

from evaluation.agent_parity import ParityCase, run_parity_case, summarize
from models.ai_extraction import AIExtraction
from models.company import Company
from models.document_chunk import EMBEDDING_DIM, DocumentChunk
from models.earnings_event import EarningsEvent
from models.enums import FilingType
from models.filing import Filing
from schemas.agent import (
    IntentCategory,
    IntentClassification,
    ToolPlan,
    ToolPlanItem,
    VerificationResult,
)
from schemas.extraction import EPSGuidance, GuidanceExtraction, RevenueGuidance
from services.extraction import EXTRACTION_TYPE_GUIDANCE
from services.llm.types import GenerateResult, TokenUsage

NOW = datetime.now(UTC)


@pytest.fixture
def corpus_company(db_session) -> Company:
    """One real company with filing chunks, earnings events and two
    guidance extractions -- enough for every intent in the corpus to find
    genuine evidence."""
    company = Company(ticker="ZZPAR1", name="ZZ Parity Inc", cik="0009993001")
    db_session.add(company)
    db_session.flush()

    for index, (filing_date, accession) in enumerate(
        [(date(2025, 11, 12), "ZZPAR1-Q3"), (date(2026, 2, 18), "ZZPAR1-Q4")]
    ):
        filing = Filing(
            company_id=company.id,
            filing_type=FilingType.FORM_10Q,
            filing_date=filing_date,
            accession_number=accession,
            source_url=f"https://example.com/{accession}.htm",
            retrieved_at=NOW,
        )
        db_session.add(filing)
        db_session.flush()
        for chunk_index in range(3):
            db_session.add(
                DocumentChunk(
                    filing_id=filing.id,
                    company_id=company.id,
                    chunk_index=chunk_index,
                    section="Item 1A" if chunk_index == 0 else "Item 7",
                    text=(
                        f"Supply concentration remains a risk factor. Gross margin "
                        f"commentary {index}-{chunk_index}."
                    ),
                    token_count=12,
                    embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
                    embedding_model="stub",
                    retrieved_at=NOW,
                )
            )
        db_session.add(
            AIExtraction(
                company_id=company.id,
                filing_id=filing.id,
                extraction_type=EXTRACTION_TYPE_GUIDANCE,
                # Real GuidanceExtraction shape (schemas/extraction.py), so
                # compare_guidance exercises the genuine deterministic
                # comparison rather than a payload it has to reject.
                extracted_data=GuidanceExtraction(
                    revenue=RevenueGuidance(low="100", high=f"{110 + index * 5}"),
                    eps=EPSGuidance(low="1.00", high=f"{1.10 + index * 0.05:.2f}"),
                    key_drivers=["datacenter demand"],
                ).model_dump(mode="json"),
                source_chunk_ids=[],
                model="stub",
                prompt_version="v1",
                retrieved_at=NOW,
            )
        )

    for quarter, event_date in enumerate(
        [date(2025, 3, 20), date(2025, 6, 25), date(2025, 9, 24), date(2025, 12, 18)], start=1
    ):
        db_session.add(
            EarningsEvent(
                company_id=company.id,
                earnings_date=event_date,
                fiscal_quarter=quarter,
                fiscal_year=2025,
            )
        )
    db_session.flush()
    return company


def _script(intent: IntentCategory, tool: str, arguments: dict, *, supported: bool = True):
    """Builds a fresh, unconsumed provider. Called twice per case so each
    runtime starts from an identical script."""

    def _factory() -> _ScriptedLLM:
        return _ScriptedLLM(
            supports_tool_calling=False,
            structured_responses={
                IntentClassification: [
                    IntentClassification(category=intent, reasoning="corpus case")
                ],
                ToolPlan: [
                    ToolPlan(items=[ToolPlanItem(tool_name=tool, arguments=arguments)])
                ],
                VerificationResult: [
                    VerificationResult(supported=supported, unsupported_claims=[]),
                    VerificationResult(supported=True),
                ],
            },
            generate_responses=[
                GenerateResult(
                    content=f"A grounded answer for the {intent.value} case [1].",
                    usage=TokenUsage(input_tokens=120, output_tokens=40),
                ),
                GenerateResult(
                    content="A revised, grounded answer [1].",
                    usage=TokenUsage(input_tokens=130, output_tokens=35),
                ),
            ],
        )

    return _factory


CORPUS = [
    (
        "analyze upcoming earnings",
        IntentCategory.EARNINGS_HISTORY,
        "get_historical_earnings",
        {},
    ),
    (
        "summarize filing risk factors",
        IntentCategory.FILING_RESEARCH,
        "search_filings",
        {"query": "risk factors"},
    ),
    (
        "compare recent guidance",
        IntentCategory.GUIDANCE_COMPARISON,
        "compare_guidance",
        {},
    ),
    (
        "explain the implied move",
        IntentCategory.OPTIONS_ANALYTICS,
        "calculate_implied_move",
        {
            "underlying_price": "100",
            "strike": "100",
            "call_price": "3.20",
            "put_price": "2.90",
            "expiration_label": "2026-03-20",
        },
    ),
    (
        "retrieve evidence for margin trend",
        IntentCategory.GENERAL,
        "search_filings",
        {"query": "gross margin trend"},
    ),
]


@pytest.mark.parametrize(("name", "intent", "tool", "arguments"), CORPUS)
def test_the_two_runtimes_agree_structurally(
    db_session, corpus_company, name, intent, tool, arguments
):
    factory = _script(intent, tool, arguments)
    result = run_parity_case(
        db_session,
        ParityCase(
            name=name,
            question=name,
            resolved_tickers=[corpus_company.ticker],
            company_id=corpus_company.id,
        ),
        legacy_llm=factory(),
        graph_llm=factory(),
        embedder=_StubEmbedder(),
    )

    assert result.passed, f"{name}: {result.unexpected}\n{result.as_dict()}"
    # Any difference that DID appear must be one the graph declares, and
    # its precondition must genuinely have held.
    for field_name in result.declared:
        assert field_name in {
            "tools_called",
            "tools_succeeded",
            "citation_count",
            "verification_supported",
        }


def test_the_graph_does_not_inflate_the_llm_call_budget(db_session, corpus_company):
    """Requirement 47, as a number rather than an impression: 2 calls must
    not become 12."""
    deltas = []
    for name, intent, tool, arguments in CORPUS:
        factory = _script(intent, tool, arguments)
        result = run_parity_case(
            db_session,
            ParityCase(
                name=name,
                question=name,
                resolved_tickers=[corpus_company.ticker],
                company_id=corpus_company.id,
            ),
            legacy_llm=factory(),
            graph_llm=factory(),
            embedder=_StubEmbedder(),
        )
        deltas.append((name, result.llm_call_delta, result.tool_call_delta))

    for name, llm_delta, tool_delta in deltas:
        # No revision happens in this corpus, so the re-verification path
        # is never taken and the budget must be identical.
        assert llm_delta == 0, f"{name} changed the LLM budget by {llm_delta}"
        # Requirement 48: a targeted retrieval round may legitimately add
        # ONE tool call, and nothing may add more than that.
        assert 0 <= tool_delta <= 1, f"{name} changed the tool budget by {tool_delta}"


def test_a_revision_happens_in_both_runtimes_or_neither(db_session, corpus_company):
    factory = _script(
        IntentCategory.FILING_RESEARCH, "search_filings", {"query": "risk"}, supported=False
    )
    result = run_parity_case(
        db_session,
        ParityCase(
            name="unsupported draft",
            question="summarize filing risk factors",
            resolved_tickers=[corpus_company.ticker],
            company_id=corpus_company.id,
        ),
        legacy_llm=factory(),
        graph_llm=factory(),
        embedder=_StubEmbedder(),
    )
    assert result.legacy.revised == result.langgraph.revised is True
    assert result.passed, result.unexpected
    # The one intended divergence: legacy reports the PRE-revision verdict
    # about an answer it already replaced; the graph re-verifies and reports
    # the verdict on what it actually returns.
    assert result.legacy.verification_supported is False
    assert result.langgraph.verification_supported is True
    assert "verification_supported" in result.declared
    # And it costs exactly one extra call, only on this path.
    assert result.llm_call_delta == 1


def test_the_summary_shape_is_what_the_report_table_needs(db_session, corpus_company):
    results = []
    for name, intent, tool, arguments in CORPUS[:2]:
        factory = _script(intent, tool, arguments)
        results.append(
            run_parity_case(
                db_session,
                ParityCase(
                    name=name,
                    question=name,
                    resolved_tickers=[corpus_company.ticker],
                    company_id=corpus_company.id,
                ),
                legacy_llm=factory(),
                graph_llm=factory(),
                embedder=_StubEmbedder(),
            )
        )
    summary = summarize(results)
    assert summary["cases"] == 2
    assert summary["passed"] == 2
    assert summary["failed"] == []
    assert summary["max_llm_call_delta"] == 0
