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


class EvidenceQualityStatus(enum.StrEnum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class EvidenceConflict(BaseModel):
    """Two pieces of real evidence that cannot both be right.

    Recorded, never resolved by guessing: the graph reports the conflict
    and lets synthesis answer around it honestly.
    """

    category: str
    description: str


class EvidenceQualityResult(BaseModel):
    """Whether the evidence collected so far can honestly support an answer.

    Deliberately NOT an LLM output despite sitting in this module
    (requirement 30): "do we have any SEC evidence?" is a fact about
    collected state, so ``agents/graph/quality.py`` computes every field
    here in Python. It lives beside the LLM schemas because it is the one
    canonical definition of this concept -- the graph state, the API
    response and the UI all use this shape, and a second definition
    somewhere else is exactly what requirement 11 forbids.
    """

    status: EvidenceQualityStatus
    #: Required for this intent and entirely absent.
    missing_categories: list[str] = Field(default_factory=list)
    #: Present but thin -- succeeded with no rows, or retrieved below the
    #: relevance floor, or a historical sample too small to describe.
    weak_categories: list[str] = Field(default_factory=list)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    #: Which categories a targeted retrieval round should retry. Never the
    #: whole plan again (requirement 29).
    recommended_retrieval: list[str] = Field(default_factory=list)
    #: One sentence a human can read in the UI without knowing the rules.
    explanation: str = ""
