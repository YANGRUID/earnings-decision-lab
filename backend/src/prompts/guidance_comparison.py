"""Versioned prompt for LLM-judged textual comparison of management
commentary across two quarters. Deliberately separate from
analytics.earnings.guidance_comparison, which does the numeric (revenue/
margin/capex midpoint) comparison deterministically — see that module's
docstring for why the two are never mixed.
"""

PROMPT_VERSION = "guidance-comparison-v1"

SYSTEM_PROMPT = """\
You are comparing a company's management commentary between two fiscal quarters.

You will be given the extracted key drivers, risks, and important topics from the PREVIOUS \
quarter and the CURRENT quarter. Identify:
- new_positive_themes: topics or drivers that are new in the current quarter and framed \
positively (e.g. new growth areas, favorable trends).
- new_negative_themes: topics or risks that are new in the current quarter and framed \
negatively (e.g. new risks, headwinds).
- removed_themes: topics that appeared in the previous quarter's commentary but are absent \
from the current quarter's.

Base this ONLY on the two provided lists. Do not use outside knowledge of the company or \
industry. A theme must be genuinely absent to count as "removed" — near-synonyms of a \
still-present theme are not removed.
"""


def build_comparison_user_prompt(previous_themes: list[str], current_themes: list[str]) -> str:
    previous = "\n".join(f"- {t}" for t in previous_themes) or "(none)"
    current = "\n".join(f"- {t}" for t in current_themes) or "(none)"
    return f"PREVIOUS quarter themes:\n{previous}\n\nCURRENT quarter themes:\n{current}"
