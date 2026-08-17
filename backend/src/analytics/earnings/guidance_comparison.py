"""Deterministic quarter-over-quarter guidance comparison.

Every number here is arithmetic on two already-extracted
``_RangeGuidance``-shaped values (schemas.extraction) — never an LLM call.
The LLM-judged comparison of management *commentary themes* is a separate
concern (services.extraction.compare_commentary_themes, using
prompts/guidance_comparison.py) — deliberately kept apart so a numeric
midpoint change is always exact arithmetic, never a model's paraphrase of
"revenue guidance went up a bit."
"""

from dataclasses import dataclass
from decimal import Decimal

from schemas.extraction import EPSGuidance, GrossMarginGuidance, RevenueGuidance


@dataclass(frozen=True)
class RangeComparison:
    previous_low: Decimal | None
    previous_high: Decimal | None
    previous_midpoint: Decimal | None
    current_low: Decimal | None
    current_high: Decimal | None
    current_midpoint: Decimal | None
    midpoint_change: Decimal | None
    midpoint_change_pct: Decimal | None


def compare_ranges(
    previous: RevenueGuidance | EPSGuidance | GrossMarginGuidance | None,
    current: RevenueGuidance | EPSGuidance | GrossMarginGuidance | None,
) -> RangeComparison:
    prev_mid = previous.midpoint if previous else None
    curr_mid = current.midpoint if current else None

    midpoint_change = None
    midpoint_change_pct = None
    if prev_mid is not None and curr_mid is not None:
        midpoint_change = curr_mid - prev_mid
        if prev_mid != 0:
            midpoint_change_pct = midpoint_change / abs(prev_mid)

    return RangeComparison(
        previous_low=previous.low if previous else None,
        previous_high=previous.high if previous else None,
        previous_midpoint=prev_mid,
        current_low=current.low if current else None,
        current_high=current.high if current else None,
        current_midpoint=curr_mid,
        midpoint_change=midpoint_change,
        midpoint_change_pct=midpoint_change_pct,
    )


@dataclass(frozen=True)
class GuidanceComparison:
    revenue: RangeComparison
    eps: RangeComparison
    gross_margin: RangeComparison
    capex: RangeComparison


def compare_guidance(previous_extraction, current_extraction) -> GuidanceComparison:
    """Both arguments are ``schemas.extraction.GuidanceExtraction`` instances
    (or anything with matching .revenue/.eps/.gross_margin/.capex attrs)."""
    return GuidanceComparison(
        revenue=compare_ranges(previous_extraction.revenue, current_extraction.revenue),
        eps=compare_ranges(previous_extraction.eps, current_extraction.eps),
        gross_margin=compare_ranges(
            previous_extraction.gross_margin, current_extraction.gross_margin
        ),
        capex=compare_ranges(previous_extraction.capex, current_extraction.capex),
    )
