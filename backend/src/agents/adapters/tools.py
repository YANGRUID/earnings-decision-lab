"""Existing research services, exposed as LangChain tools.

Thin by construction (requirement 14): every adapter below calls exactly
one already-real, already-tested ``agents.tools.base.Tool`` and returns
what it returned. No finance logic moves into a decorator -- an adapter
that computed anything would be a second implementation of something this
project already has one of.

Three structural guarantees, enforced here rather than promised in a doc:

1. Every exposed tool carries an explicit access classification
   (requirement 15). A tool with no entry in ``TOOL_ACCESS`` raises at
   registry-build time, so adding a tool and forgetting to classify it
   fails loudly instead of defaulting to "probably fine".
2. Nothing classified ``WRITE`` is exposed to the graph. Production
   evidence writes stay in application code that the graph calls, never in
   a tool the model can decide to invoke.
3. No brokerage-execution tool can exist here at all (requirement 16).
   ``_assert_no_execution_tool`` refuses the name shapes outright, so this
   boundary survives someone adding a tool in good faith later.
"""

import enum
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agents.tools.base import Tool
from agents.tools.registry import build_tool_registry
from agents.tools.types import ToolOutcome
from rag.embeddings import EmbeddingProvider


class ToolAccess(enum.StrEnum):
    #: Reads persisted rows or already-ingested documents. No mutation.
    READ_ONLY = "read_only"
    #: Pure deterministic computation over values passed in. No I/O at all.
    DERIVED_CALCULATION = "derived_calculation"
    #: Mutates state. Never exposed to the graph -- see module docstring.
    WRITE = "write"


class EvidenceCategory(enum.StrEnum):
    """What KIND of evidence a tool produces.

    The evidence-quality gate (agents/graph/quality.py) reasons in these
    terms rather than in tool names, so a second filing-search tool added
    later counts toward the same category without the gate changing.
    """

    FILING = "filing"
    EARNINGS_HISTORY = "earnings_history"
    ESTIMATES = "estimates"
    GUIDANCE = "guidance"
    OPTIONS_CONTEXT = "options_context"
    DERIVED = "derived"


#: Access classification per existing tool (agents/tools/registry.py).
#: Verified against each tool's own ``run`` -- none of the seven performs a
#: ``db.add``/``commit``/``flush``/``delete``, which is itself asserted by
#: tests/test_agent_tool_adapters.py rather than taken on trust.
TOOL_ACCESS: dict[str, ToolAccess] = {
    "search_filings": ToolAccess.READ_ONLY,
    "get_historical_earnings": ToolAccess.READ_ONLY,
    "get_analyst_estimates": ToolAccess.READ_ONLY,
    "compare_guidance": ToolAccess.READ_ONLY,
    "get_options_snapshot": ToolAccess.READ_ONLY,
    "calculate_strategy_payoff": ToolAccess.DERIVED_CALCULATION,
    "calculate_implied_move": ToolAccess.DERIVED_CALCULATION,
}

TOOL_EVIDENCE_CATEGORY: dict[str, EvidenceCategory] = {
    "search_filings": EvidenceCategory.FILING,
    "get_historical_earnings": EvidenceCategory.EARNINGS_HISTORY,
    "get_analyst_estimates": EvidenceCategory.ESTIMATES,
    "compare_guidance": EvidenceCategory.GUIDANCE,
    "get_options_snapshot": EvidenceCategory.OPTIONS_CONTEXT,
    "calculate_strategy_payoff": EvidenceCategory.DERIVED,
    "calculate_implied_move": EvidenceCategory.DERIVED,
}

#: Name fragments that may never appear on a tool reachable from the
#: research graph. Deliberately substring matching, not exact names: the
#: point is to refuse the whole shape, including a well-meaning
#: ``submit_bracket_order_dry_run``.
_FORBIDDEN_TOOL_FRAGMENTS = (
    "place_order",
    "submit_order",
    "modify_order",
    "cancel_order",
    "exercise",
    "place_trade",
    "execute_trade",
    "bracket_order",
)


class ExecutionToolRefused(RuntimeError):
    """A tool whose name describes brokerage execution was offered to the
    research graph. This is a structural refusal, not a configuration
    error -- see requirement 16 and docs/agentic_research_architecture.md.
    """


def _assert_no_execution_tool(name: str) -> None:
    lowered = name.lower()
    for fragment in _FORBIDDEN_TOOL_FRAGMENTS:
        if fragment in lowered:
            raise ExecutionToolRefused(
                f"tool {name!r} looks like brokerage execution; the agentic research "
                f"layer never places, modifies, cancels or exercises anything"
            )


def access_of(name: str) -> ToolAccess:
    try:
        return TOOL_ACCESS[name]
    except KeyError:
        raise ExecutionToolRefused(
            f"tool {name!r} has no access classification in TOOL_ACCESS -- classify it "
            f"READ_ONLY, DERIVED_CALCULATION or WRITE before exposing it to the graph"
        ) from None


def as_langchain_tool(tool: Tool[Any]) -> StructuredTool:
    """One ``Tool`` -> one ``StructuredTool``, calling ``tool.run`` once.

    The LangChain content is the outcome's own one-line summary; the full
    ``ToolOutcome`` (structured data and citations) rides along as the
    message artifact, so the graph keeps real citations instead of
    re-parsing them out of prose.
    """
    _assert_no_execution_tool(tool.name)
    args_schema: type[BaseModel] = tool.args_schema

    def _run(**kwargs: Any) -> tuple[str, ToolOutcome]:
        outcome = tool.run(args_schema.model_validate(kwargs))
        return outcome.summary, outcome

    return StructuredTool.from_function(
        func=_run,
        name=tool.name,
        description=tool.description,
        args_schema=args_schema,
        response_format="content_and_artifact",
        # The graph records a failed tool call honestly (see
        # agents/graph/nodes.py); it must never receive a string that
        # reads like evidence but is actually an exception repr.
        handle_tool_error=False,
    )


def build_langchain_tools(
    db: Session, embedder: EmbeddingProvider
) -> tuple[list[StructuredTool], dict[str, Tool[Any]]]:
    """The LangChain-facing registry, derived from the existing one.

    Returns both the adapted tools and the underlying registry, because the
    graph still needs the underlying ``Tool`` objects for the deterministic
    argument defaulting that the legacy orchestrator applies
    (``_apply_deterministic_defaults``) -- that is a behavioural guarantee,
    not a framework detail, so it is preserved rather than reinvented.
    """
    registry = build_tool_registry(db, embedder)
    adapted: list[StructuredTool] = []
    for name, tool in registry.items():
        if access_of(name) is ToolAccess.WRITE:
            continue
        adapted.append(as_langchain_tool(tool))
    return adapted, registry


def tool_names(tools: list[StructuredTool] | list[BaseTool]) -> list[str]:
    return [t.name for t in tools]


__all__ = [
    "EvidenceCategory",
    "ExecutionToolRefused",
    "TOOL_ACCESS",
    "TOOL_EVIDENCE_CATEGORY",
    "ToolAccess",
    "access_of",
    "as_langchain_tool",
    "build_langchain_tools",
    "tool_names",
]
