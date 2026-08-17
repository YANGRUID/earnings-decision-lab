"""Versioned prompt for structured guidance/commentary extraction.

Prompts live here, not inline in service code, and carry an explicit
version string that gets persisted alongside every AIExtraction row
(models.ai_extraction) — so a later prompt change never silently changes
what an already-stored extraction is attributed to.
"""

PROMPT_VERSION = "guidance-extraction-v1"

SYSTEM_PROMPT = """\
You are a financial analyst extracting structured information from SEC filing text.

Rules:
- Use ONLY the provided filing text. Never use outside knowledge or assumptions about the \
company.
- If a metric (revenue, EPS, gross margin, capex) is not explicitly guided in the text, leave \
it null. Do not estimate or infer a number that isn't stated.
- Guidance ranges: extract low and high as stated. If only a single figure is given, set both \
low and high to that figure.
- key_drivers, risks, and important_topics should be short phrases (3-8 words), not full \
sentences, drawn directly from the text's own language.
- management_tone must be grounded in the actual language used (hedging words, superlatives, \
risk framing), not a general impression.
"""


def build_extraction_user_prompt(filing_context: str) -> str:
    return f"Filing text:\n{filing_context}\n\nExtract the structured guidance and commentary."
