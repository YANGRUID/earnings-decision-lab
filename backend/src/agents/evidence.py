"""Behavioural helpers shared by BOTH agent runtimes.

Extracted verbatim from ``agents/orchestrator.py`` (where they lived as
``_apply_deterministic_defaults`` and ``_assemble_evidence``) so the legacy
orchestrator and the LangGraph workflow run the *same* code rather than two
implementations that merely agree today. Parity between the two runtimes
(requirements 44-46) is then structural: a change to the deterministic
argument defaulting or the evidence-block format moves both at once, and
the parity harness cannot be quietly invalidated by editing one copy.

No behaviour is changed by the move. The docstrings below are the originals.
"""

import json
from datetime import date
from typing import Any

from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from agents.types import ToolCallRecord
from rag.context import Citation


def apply_deterministic_defaults(
    tool: Tool[Any],
    arguments: dict,
    resolved_tickers: list[str] | None,
    as_of: date | None,
) -> dict:
    """Post-live correction (2026-08-25) Part A5/A8 -- a real, deterministic
    safety net, not just a prompt hint: if this question was already
    resolved to exactly one real company, a tool call that takes a
    ``ticker`` argument but the LLM left blank is scoped to that company
    rather than silently searching unscoped (which is exactly how a
    single-company question could otherwise let semantically-similar
    documents from an unrelated company become primary evidence -- the
    real Part A5 concern). Deliberately does NOT override a ticker the
    LLM DID supply, even if it differs from the resolved one -- a
    genuinely multi-company question (Part A6) must still let the LLM
    call the same tool once per company. Same mechanism for ``as_of``
    (Part A8), applied whenever the tool schema has that field, since a
    missing point-in-time cutoff is never itself evidence of intent to
    ignore it -- an omitted arg and an intentional "no cutoff" look
    identical to the LLM either way, so this project's own real caller
    (not the LLM) is the source of truth for whether one applies at all.
    """
    fields = tool.args_schema.model_fields
    result = dict(arguments)
    if (
        resolved_tickers
        and len(resolved_tickers) == 1
        and "ticker" in fields
        and not result.get("ticker")
    ):
        result["ticker"] = resolved_tickers[0]
    if as_of is not None and "as_of" in fields and not result.get("as_of"):
        result["as_of"] = as_of.isoformat()
    return result


def assemble_evidence(
    tool_results: list[tuple[ToolCallRecord, ToolOutcome | None]],
) -> tuple[str, list[Citation]]:
    """Builds the evidence text handed to synthesis/verification, and the
    final citation list. Filing-search results keep their [N] markers as
    produced by rag.context.assemble_context; if more than one filing-search
    call happens in a single query (uncommon but possible), each is kept in
    its own clearly-labeled block rather than globally renumbered — a
    documented simplification, see docs/ai_architecture.md.
    """
    blocks: list[str] = []
    citations: list[Citation] = []
    for record, outcome in tool_results:
        blocks.append(evidence_block_text(record, outcome))
        if outcome is not None and outcome.success and outcome.citations:
            citations.extend(outcome.citations)
    return "\n\n".join(blocks), citations


def evidence_block_text(record: ToolCallRecord, outcome: ToolOutcome | None) -> str:
    """One tool's evidence block, in the exact format both runtimes send to
    the model. Split out of ``assemble_evidence`` so the graph can label and
    measure each block individually without re-deriving its text."""
    if outcome is None:
        return f"### {record.tool_name} — FAILED\n{record.error}"
    if not outcome.success:
        return f"### {record.tool_name}\n{outcome.error or outcome.summary}"
    if outcome.citations:
        return (
            f"### {record.tool_name}\n{outcome.summary}\n"
            f"{outcome.data.get('context_text', '')}"
        )
    return (
        f"### {record.tool_name}\n{outcome.summary}\n"
        f"Data: {json.dumps(outcome.data, default=str)}"
    )
