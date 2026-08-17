"""Structured extraction schemas for guidance and management commentary.

These are the ``schema`` argument to ``LLMProvider.generate_structured`` —
see docs/llm_providers.md for how structured output is normalized across
providers. Every numeric field is optional: a filing that doesn't state
guidance for a given metric should produce ``None`` there, not a
model-invented number.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class _RangeGuidance(BaseModel):
    low: Decimal | None = None
    high: Decimal | None = None
    period: str | None = Field(default=None, description="e.g. 'Q1 FY2027' or 'FY2027'")

    @model_validator(mode="after")
    def _check_order(self) -> "_RangeGuidance":
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("low must be <= high")
        return self

    @property
    def midpoint(self) -> Decimal | None:
        if self.low is not None and self.high is not None:
            return (self.low + self.high) / 2
        return self.low if self.low is not None else self.high


class RevenueGuidance(_RangeGuidance):
    """Values in the filing's reporting currency, millions unless the
    filing states otherwise — this schema does not itself normalize units;
    that's a deterministic concern handled at comparison time, not by the
    LLM extracting the raw stated figures."""


class EPSGuidance(_RangeGuidance):
    pass


class GrossMarginGuidance(_RangeGuidance):
    """``low``/``high`` are percentages, e.g. 38.5 for 38.5%."""


class CapexGuidance(_RangeGuidance):
    pass


class ManagementTone(BaseModel):
    overall: Literal["positive", "neutral", "negative", "mixed"]
    summary: str = Field(description="One or two sentences, grounded in the provided text only.")


class GuidanceExtraction(BaseModel):
    """One extraction result for a single filing/section. Persisted as
    AIExtraction.extracted_data — see models.ai_extraction.
    """

    revenue: RevenueGuidance | None = None
    eps: EPSGuidance | None = None
    gross_margin: GrossMarginGuidance | None = None
    capex: CapexGuidance | None = None
    key_drivers: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    management_tone: ManagementTone | None = None
    important_topics: list[str] = Field(default_factory=list)


class GuidanceComparisonThemes(BaseModel):
    """LLM-judged textual comparison of management commentary across two
    periods — kept separate from the deterministic numeric comparison in
    analytics.earnings.guidance_comparison, per this project's rule that
    arithmetic is never delegated to a model."""

    new_positive_themes: list[str] = Field(default_factory=list)
    new_negative_themes: list[str] = Field(default_factory=list)
    removed_themes: list[str] = Field(
        default_factory=list, description="Themes present last period but absent this period."
    )
