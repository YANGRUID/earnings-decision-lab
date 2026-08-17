"""Structured-output schemas for the agent orchestration pipeline
(agents/orchestrator.py). Each maps to one distinct pipeline stage — intent
classification, the structured-planner fallback for providers without
native tool calling, and verification.
"""

import enum

from pydantic import BaseModel, Field


class IntentCategory(enum.StrEnum):
    EARNINGS_HISTORY = "earnings_history"
    FILING_RESEARCH = "filing_research"
    GUIDANCE_COMPARISON = "guidance_comparison"
    OPTIONS_ANALYTICS = "options_analytics"
    GENERAL = "general"


class IntentClassification(BaseModel):
    category: IntentCategory
    reasoning: str = Field(description="One sentence: why this category fits the question.")


class ToolPlanItem(BaseModel):
    tool_name: str
    arguments: dict


class ToolPlan(BaseModel):
    """Explicit plan produced by the structured-planner fallback (used when
    the configured provider doesn't support native tool calling — see
    agents/orchestrator.py). Native tool calling produces the equivalent
    information via GenerateResult.tool_calls instead of this schema; both
    paths converge on the same list[ToolPlanItem]-shaped plan before
    execution.
    """

    items: list[ToolPlanItem] = Field(default_factory=list)


class VerificationResult(BaseModel):
    supported: bool = Field(
        description="True only if every factual claim in the draft answer is backed by the "
        "provided evidence."
    )
    unsupported_claims: list[str] = Field(default_factory=list)
    notes: str = ""
