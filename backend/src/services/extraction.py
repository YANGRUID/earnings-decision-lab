"""Structured extraction service: retrieves filing context, calls the
configured LLM for structured output, persists an AIExtraction row with
full provenance (source chunks, model, prompt version).
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from models.ai_extraction import AIExtraction
from models.document_chunk import DocumentChunk
from prompts.guidance_comparison import PROMPT_VERSION as COMPARISON_PROMPT_VERSION
from prompts.guidance_comparison import SYSTEM_PROMPT as COMPARISON_SYSTEM_PROMPT
from prompts.guidance_comparison import build_comparison_user_prompt
from prompts.guidance_extraction import PROMPT_VERSION as EXTRACTION_PROMPT_VERSION
from prompts.guidance_extraction import SYSTEM_PROMPT as EXTRACTION_SYSTEM_PROMPT
from prompts.guidance_extraction import build_extraction_user_prompt
from schemas.extraction import GuidanceComparisonThemes, GuidanceExtraction
from services.llm.base import LLMProvider
from services.llm.types import ChatMessage

EXTRACTION_TYPE_GUIDANCE = "guidance"


def extract_guidance(
    db: Session,
    llm: LLMProvider,
    filing_id: int,
    company_id: int,
    chunks: list[DocumentChunk],
) -> AIExtraction:
    """Runs structured guidance extraction over ``chunks`` (typically the
    MD&A / risk-factors sections of one filing) and persists the result.
    """
    context = "\n\n".join(f"[{c.section or 'unlabeled'}]\n{c.text}" for c in chunks)
    messages = [
        ChatMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=build_extraction_user_prompt(context)),
    ]
    result: GuidanceExtraction = llm.generate_structured(
        messages, GuidanceExtraction, temperature=0.0, max_tokens=1500
    )

    row = AIExtraction(
        filing_id=filing_id,
        company_id=company_id,
        extraction_type=EXTRACTION_TYPE_GUIDANCE,
        extracted_data=result.model_dump(mode="json"),
        source_chunk_ids=[c.id for c in chunks],
        model=llm.model,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        confidence=None,  # LLM self-reported confidence isn't reliable enough to store as fact
        retrieved_at=datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    return row


def compare_commentary_themes(
    llm: LLMProvider,
    previous: GuidanceExtraction,
    current: GuidanceExtraction,
) -> GuidanceComparisonThemes:
    """LLM-judged comparison of commentary *themes* only — numeric guidance
    comparison is analytics.earnings.guidance_comparison.compare_guidance,
    deterministic and never delegated to a model. Kept as a separate
    function/prompt/schema entirely, not a shared code path, so the two
    concerns can't accidentally blend.
    """
    previous_themes = previous.key_drivers + previous.risks + previous.important_topics
    current_themes = current.key_drivers + current.risks + current.important_topics

    messages = [
        ChatMessage(role="system", content=COMPARISON_SYSTEM_PROMPT),
        ChatMessage(
            role="user", content=build_comparison_user_prompt(previous_themes, current_themes)
        ),
    ]
    return llm.generate_structured(
        messages, GuidanceComparisonThemes, temperature=0.0, max_tokens=800
    )


__all__ = [
    "EXTRACTION_TYPE_GUIDANCE",
    "COMPARISON_PROMPT_VERSION",
    "extract_guidance",
    "compare_commentary_themes",
]
