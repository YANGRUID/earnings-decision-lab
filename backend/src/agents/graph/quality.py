"""The evidence-quality gate: can this evidence honestly support an answer?

**Entirely deterministic.** Not one function here asks a model anything
(requirement 30). "Do we have SEC evidence?", "did that tool succeed with
zero rows?", "is this filing dated after the run's own cutoff?" are facts
about collected state, and a model asked to judge them would be a slower,
more expensive and less reliable way to read a list. The LLM-call budget
of this gate is zero, which is also what keeps the graph's total budget
equal to the legacy orchestrator's (requirement 47).

Intent-specific by construction (requirement 27): a question about
historical earnings moves is not short of evidence because no one fetched
an options chain, so each intent declares only the categories it genuinely
needs.

Where LLM judgement *would* genuinely help -- semantic contradiction
between two filings' prose -- this gate deliberately reports nothing
rather than guessing. See ``docs/agentic_research_architecture.md``:
semantic conflict detection is a named, deferred extension, not a silent
gap, and inventing a conflict would be worse than reporting none.
"""

from collections.abc import Sequence
from datetime import date

from agents.adapters.tools import EvidenceCategory
from agents.graph.state import CitationRef, EvidenceBlock, ToolRecord
from schemas.agent import (
    EvidenceConflict,
    EvidenceQualityResult,
    EvidenceQualityStatus,
    IntentCategory,
)

#: Categories an intent cannot be answered without.
REQUIRED_BY_INTENT: dict[str, frozenset[EvidenceCategory]] = {
    IntentCategory.EARNINGS_HISTORY: frozenset({EvidenceCategory.EARNINGS_HISTORY}),
    IntentCategory.FILING_RESEARCH: frozenset({EvidenceCategory.FILING}),
    IntentCategory.GUIDANCE_COMPARISON: frozenset({EvidenceCategory.GUIDANCE}),
    IntentCategory.OPTIONS_ANALYTICS: frozenset({EvidenceCategory.OPTIONS_CONTEXT}),
    # A general question has no required category: the honest bar is that
    # *something* produced real data, checked separately below.
    IntentCategory.GENERAL: frozenset(),
}

#: Intents whose answers are expected to quote a source. A filing answer
#: with no citation is not an answer this project is willing to give.
CITATIONS_REQUIRED_BY_INTENT: frozenset[str] = frozenset(
    {IntentCategory.FILING_RESEARCH, IntentCategory.GUIDANCE_COMPARISON}
)

#: Fewer than this many past events cannot describe a "pattern" honestly --
#: four quarters is one full fiscal year, the smallest sample this project's
#: own historical-move statistics are willing to characterise.
MIN_HISTORICAL_SAMPLE = 4


def _blocks_by_category(blocks: list[EvidenceBlock]) -> dict[str, list[EvidenceBlock]]:
    out: dict[str, list[EvidenceBlock]] = {}
    for block in blocks:
        out.setdefault(block["category"], []).append(block)
    return out


def _point_in_time_conflicts(
    citations: Sequence[CitationRef], as_of: date | None
) -> list[EvidenceConflict]:
    """A citation dated after the run's own cutoff.

    This is a genuine contradiction, not a style nit: the run declared a
    point-in-time bound and the evidence violates it, so either the bound
    or the evidence is wrong. Retrieval filters on ``filing_date_to``
    already, so reaching this means something bypassed the filter -- worth
    surfacing loudly rather than trusting the filter silently.
    """
    if as_of is None:
        return []
    conflicts: list[EvidenceConflict] = []
    for citation in citations:
        raw = citation.get("filing_date")
        if not raw:
            continue
        if date.fromisoformat(raw) > as_of:
            conflicts.append(
                EvidenceConflict(
                    category=EvidenceCategory.FILING.value,
                    description=(
                        f"{citation.get('filing_type', 'filing')} dated {raw} is after this "
                        f"run's evidence cutoff of {as_of.isoformat()}"
                    ),
                )
            )
    return conflicts


def _event_date_conflicts(
    blocks: list[EvidenceBlock], calendar_earnings_date: date | None
) -> list[EvidenceConflict]:
    """Does the consensus provider's expected report date agree with the
    calendar event this run is about? (requirement 27, "event date/timing
    corroborated".)

    Only checked when the run actually names a calendar event -- an
    interactive question with no event has nothing to corroborate against,
    and inventing a comparison would manufacture a conflict.
    """
    if calendar_earnings_date is None:
        return []
    conflicts: list[EvidenceConflict] = []
    for block in blocks:
        reported = block.get("reported_earnings_date")
        if not reported:
            continue
        if date.fromisoformat(reported) != calendar_earnings_date:
            conflicts.append(
                EvidenceConflict(
                    category=EvidenceCategory.ESTIMATES.value,
                    description=(
                        f"consensus provider expects the report on {reported}, but the "
                        f"earnings calendar has {calendar_earnings_date.isoformat()}"
                    ),
                )
            )
    return conflicts


def assess_evidence(
    *,
    intent: str,
    tool_records: list[ToolRecord],
    evidence_blocks: list[EvidenceBlock],
    citations: Sequence[CitationRef],
    as_of: date | None = None,
    calendar_earnings_date: date | None = None,
) -> EvidenceQualityResult:
    """One deterministic pass over collected state.

    ``INSUFFICIENT`` means a required category produced nothing at all, or
    (for a general question) nothing anywhere did. ``PARTIAL`` means the
    required categories are present but something real is wrong with them
    -- thin, uncited, or contradictory. ``SUFFICIENT`` means go ahead.
    """
    required = REQUIRED_BY_INTENT.get(intent, frozenset())
    by_category = _blocks_by_category(evidence_blocks)

    missing: list[str] = []
    weak: list[str] = []

    for category in sorted(required, key=lambda c: c.value):
        blocks = by_category.get(category.value, [])
        if not blocks:
            missing.append(category.value)
            continue
        if not any(b["item_count"] > 0 for b in blocks):
            # The tool ran, succeeded, and honestly reported nothing. That
            # is weak evidence, not a failure -- and not the same thing as
            # the tool never having been called.
            weak.append(category.value)

    # "Historical sample available?" -- a success with three events is real
    # data that still cannot support a claim about a pattern.
    history_category = EvidenceCategory.EARNINGS_HISTORY.value
    for block in by_category.get(history_category, []):
        if 0 < block["item_count"] < MIN_HISTORICAL_SAMPLE and history_category not in weak:
            weak.append(history_category)

    conflicts = _point_in_time_conflicts(citations, as_of)
    conflicts.extend(_event_date_conflicts(evidence_blocks, calendar_earnings_date))

    citations_missing = intent in CITATIONS_REQUIRED_BY_INTENT and not citations

    any_real_data = any(b["item_count"] > 0 for b in evidence_blocks)
    all_tools_failed = bool(tool_records) and all(not r["success"] for r in tool_records)

    if missing or not any_real_data or all_tools_failed:
        status = EvidenceQualityStatus.INSUFFICIENT
    elif weak or conflicts or citations_missing:
        status = EvidenceQualityStatus.PARTIAL
    else:
        status = EvidenceQualityStatus.SUFFICIENT

    # Requirement 29: retry the gap, never the whole plan. A conflict is
    # deliberately NOT retried -- fetching the same contradictory rows
    # again produces the same contradiction and spends budget to do it.
    recommended = list(missing) + [c for c in weak if c not in missing]
    if citations_missing and EvidenceCategory.FILING.value not in recommended:
        recommended.append(EvidenceCategory.FILING.value)

    return EvidenceQualityResult(
        status=status,
        missing_categories=missing,
        weak_categories=weak,
        conflicts=conflicts,
        recommended_retrieval=recommended,
        explanation=_explain(status, missing, weak, conflicts, citations_missing, any_real_data),
    )


def _explain(
    status: EvidenceQualityStatus,
    missing: list[str],
    weak: list[str],
    conflicts: list[EvidenceConflict],
    citations_missing: bool,
    any_real_data: bool,
) -> str:
    """Plain English, for a user who has never read this module.

    Phrased as what the evidence does or does not cover -- never as a
    system fault, because thin evidence is an honest outcome of a real
    search, not a bug.
    """
    if status is EvidenceQualityStatus.SUFFICIENT:
        return "The evidence collected covers what this question needs."
    reasons: list[str] = []
    if not any_real_data and not missing:
        reasons.append("no source returned any data")
    if missing:
        reasons.append(f"nothing on record for {_english_list(missing)}")
    if weak:
        reasons.append(f"only thin evidence for {_english_list(weak)}")
    if citations_missing:
        reasons.append("no citable source was found")
    if conflicts:
        reasons.append(
            "two pieces of evidence disagree"
            if len(conflicts) == 1
            else f"{len(conflicts)} pieces of evidence disagree with each other"
        )
    return _sentence(reasons)


def _english_list(values: list[str]) -> str:
    readable = [v.replace("_", " ") for v in values]
    if len(readable) == 1:
        return readable[0]
    return ", ".join(readable[:-1]) + f" and {readable[-1]}"


def _sentence(reasons: list[str]) -> str:
    if not reasons:
        return "The evidence collected is incomplete."
    body = reasons[0] if len(reasons) == 1 else ", ".join(reasons[:-1]) + f", and {reasons[-1]}"
    return body[0].upper() + body[1:] + "."
