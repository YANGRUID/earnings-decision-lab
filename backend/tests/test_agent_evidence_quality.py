"""The evidence-quality gate and its targeted-retrieval route
(requirements 69, 70, 26-30).

Two halves: the gate's own verdicts, computed with no model involved at
all, and the graph route that verdict selects.
"""

from datetime import date

import pytest

from agents.adapters.tools import EvidenceCategory
from agents.graph.quality import MIN_HISTORICAL_SAMPLE, assess_evidence
from agents.graph.state import EvidenceBlock, ToolRecord
from agents.graph.workflow import route_after_quality_gate
from schemas.agent import EvidenceQualityStatus, IntentCategory


def _block(category: str, item_count: int, **extra) -> EvidenceBlock:
    block = EvidenceBlock(
        tool_name=f"tool_for_{category}",
        category=category,
        text="evidence",
        item_count=item_count,
        retrieval_round=0,
    )
    block.update(extra)  # type: ignore[typeddict-item]
    return block


def _record(tool: str, success: bool = True) -> ToolRecord:
    return ToolRecord(
        tool_name=tool,
        arguments={},
        success=success,
        duration_ms=1.0,
        summary="",
        error=None,
        query_description=None,
        evidence_category="filing",
        retrieval_round=0,
    )


def _citation(filing_date: str) -> dict:
    return {
        "marker": "[1]",
        "ticker": "MU",
        "filing_type": "10-K",
        "filing_date": filing_date,
        "section": "Item 7",
        "source_url": "https://sec.gov/x",
        "accession_number": None,
        "evidence_cutoff": None,
    }


# --- SUFFICIENT ---------------------------------------------------------


def test_filing_research_with_cited_filing_evidence_is_sufficient():
    result = assess_evidence(
        intent=IntentCategory.FILING_RESEARCH,
        tool_records=[_record("search_filings")],
        evidence_blocks=[_block(EvidenceCategory.FILING.value, 4)],
        citations=[_citation("2026-02-01")],
    )
    assert result.status is EvidenceQualityStatus.SUFFICIENT
    assert result.recommended_retrieval == []
    assert result.explanation == "The evidence collected covers what this question needs."


def test_an_intent_does_not_need_categories_it_never_asked_for():
    """Requirement 27: an earnings-history question is not short of
    evidence because nobody fetched an options chain."""
    result = assess_evidence(
        intent=IntentCategory.EARNINGS_HISTORY,
        tool_records=[_record("get_historical_earnings")],
        evidence_blocks=[_block(EvidenceCategory.EARNINGS_HISTORY.value, 8)],
        citations=[],
    )
    assert result.status is EvidenceQualityStatus.SUFFICIENT
    assert EvidenceCategory.OPTIONS_CONTEXT.value not in result.missing_categories


# --- INSUFFICIENT -------------------------------------------------------


def test_a_required_category_never_collected_is_insufficient():
    result = assess_evidence(
        intent=IntentCategory.GUIDANCE_COMPARISON,
        tool_records=[_record("search_filings")],
        evidence_blocks=[_block(EvidenceCategory.FILING.value, 3)],
        citations=[_citation("2026-02-01")],
    )
    assert result.status is EvidenceQualityStatus.INSUFFICIENT
    assert result.missing_categories == [EvidenceCategory.GUIDANCE.value]
    assert result.recommended_retrieval == [EvidenceCategory.GUIDANCE.value]


def test_every_tool_failing_is_insufficient():
    result = assess_evidence(
        intent=IntentCategory.GENERAL,
        tool_records=[_record("search_filings", success=False)],
        evidence_blocks=[],
        citations=[],
    )
    assert result.status is EvidenceQualityStatus.INSUFFICIENT
    assert "no source returned any data" in result.explanation.lower()


# --- PARTIAL ------------------------------------------------------------


def test_a_tool_that_honestly_returned_nothing_is_weak_not_missing():
    """success=True with zero rows is this project's real "no data
    available" answer -- different from never having asked."""
    result = assess_evidence(
        intent=IntentCategory.OPTIONS_ANALYTICS,
        tool_records=[_record("get_options_snapshot")],
        evidence_blocks=[
            _block(EvidenceCategory.OPTIONS_CONTEXT.value, 0),
            _block(EvidenceCategory.DERIVED.value, 1),
        ],
        citations=[],
    )
    assert result.status is EvidenceQualityStatus.PARTIAL
    assert result.missing_categories == []
    assert result.weak_categories == [EvidenceCategory.OPTIONS_CONTEXT.value]


def test_a_historical_sample_too_small_to_describe_a_pattern_is_weak():
    result = assess_evidence(
        intent=IntentCategory.EARNINGS_HISTORY,
        tool_records=[_record("get_historical_earnings")],
        evidence_blocks=[
            _block(EvidenceCategory.EARNINGS_HISTORY.value, MIN_HISTORICAL_SAMPLE - 1)
        ],
        citations=[],
    )
    assert result.status is EvidenceQualityStatus.PARTIAL
    assert result.weak_categories == [EvidenceCategory.EARNINGS_HISTORY.value]


def test_a_filing_answer_with_no_citable_source_is_partial():
    result = assess_evidence(
        intent=IntentCategory.FILING_RESEARCH,
        tool_records=[_record("search_filings")],
        evidence_blocks=[_block(EvidenceCategory.FILING.value, 2)],
        citations=[],
    )
    assert result.status is EvidenceQualityStatus.PARTIAL
    assert EvidenceCategory.FILING.value in result.recommended_retrieval


# --- conflicts ----------------------------------------------------------


def test_evidence_dated_after_the_runs_own_cutoff_is_a_conflict():
    result = assess_evidence(
        intent=IntentCategory.FILING_RESEARCH,
        tool_records=[_record("search_filings")],
        evidence_blocks=[_block(EvidenceCategory.FILING.value, 1)],
        citations=[_citation("2026-09-01")],
        as_of=date(2026, 6, 30),
    )
    assert result.status is EvidenceQualityStatus.PARTIAL
    assert len(result.conflicts) == 1
    assert "after this run's evidence cutoff" in result.conflicts[0].description


def test_a_disagreement_about_the_report_date_is_a_conflict():
    result = assess_evidence(
        intent=IntentCategory.GENERAL,
        tool_records=[_record("get_analyst_estimates")],
        evidence_blocks=[
            _block(
                EvidenceCategory.ESTIMATES.value, 1, reported_earnings_date="2026-10-20"
            )
        ],
        citations=[],
        calendar_earnings_date=date(2026, 10, 22),
    )
    assert result.status is EvidenceQualityStatus.PARTIAL
    assert "earnings calendar has 2026-10-22" in result.conflicts[0].description


def test_nothing_is_corroborated_when_the_run_names_no_event():
    """Inventing a comparison would manufacture a conflict."""
    result = assess_evidence(
        intent=IntentCategory.GENERAL,
        tool_records=[_record("get_analyst_estimates")],
        evidence_blocks=[
            _block(EvidenceCategory.ESTIMATES.value, 1, reported_earnings_date="2026-10-20")
        ],
        citations=[],
        calendar_earnings_date=None,
    )
    assert result.conflicts == []
    assert result.status is EvidenceQualityStatus.SUFFICIENT


def test_a_conflict_alone_is_not_retried():
    """Re-fetching the same contradictory rows buys the same contradiction
    and spends budget doing it."""
    result = assess_evidence(
        intent=IntentCategory.GENERAL,
        tool_records=[_record("get_analyst_estimates")],
        evidence_blocks=[
            _block(EvidenceCategory.ESTIMATES.value, 1, reported_earnings_date="2026-10-20")
        ],
        citations=[],
        calendar_earnings_date=date(2026, 10, 22),
    )
    assert result.recommended_retrieval == []
    assert route_after_quality_gate(
        {"evidence_quality": result.model_dump(mode="json"), "retrieval_attempt_count": 0}
    ) == "synthesize"


# --- the gate costs nothing ---------------------------------------------


def test_the_gate_makes_no_llm_call_at_all():
    """Requirement 30/47: the gate's whole budget is zero. Asserted by
    reading the module rather than by trusting the docstring."""
    import inspect

    import agents.graph.quality as quality

    source = inspect.getsource(quality)
    for forbidden in (
        "generate_structured",
        "generate(",
        "with_structured_output",
        "invoke(",
        "EDLChatModel",
    ):
        assert forbidden not in source, f"the quality gate must not call {forbidden}"


# --- routing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "rounds_used", "recommended", "expected"),
    [
        ("sufficient", 0, [], "synthesize"),
        ("partial", 0, ["filing"], "targeted_retrieve"),
        ("insufficient", 0, ["guidance"], "targeted_retrieve"),
        ("partial", 1, ["filing"], "synthesize"),
        ("insufficient", 1, ["guidance"], "synthesize"),
        ("partial", 0, [], "synthesize"),
    ],
)
def test_the_route_retries_the_gap_once_then_proceeds(
    status, rounds_used, recommended, expected
):
    assert (
        route_after_quality_gate(
            {
                "evidence_quality": {"status": status, "recommended_retrieval": recommended},
                "retrieval_attempt_count": rounds_used,
            }
        )
        == expected
    )
