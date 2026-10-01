"""Tool adapters (requirements 67, 15, 16) and the retriever adapter
(requirements 17-19).

The theme: these adapters must be thin, and the boundaries they claim must
be enforced by code rather than by the docstring that claims them.
"""

import pytest
from _agent_fakes import _StubEmbedder
from pydantic import BaseModel

from agents.adapters.retriever import EDLHybridRetriever, RetrievalScope, chunk_to_document
from agents.adapters.tools import (
    TOOL_ACCESS,
    TOOL_EVIDENCE_CATEGORY,
    ExecutionToolRefused,
    ToolAccess,
    access_of,
    as_langchain_tool,
    build_langchain_tools,
    tool_names,
)
from agents.tools.base import Tool
from agents.tools.registry import build_tool_registry
from agents.tools.types import ToolOutcome
from rag.retrieval import RetrievedChunk


class _Args(BaseModel):
    ticker: str = "MU"


class _CountingTool(Tool[_Args]):
    name = "get_historical_earnings"
    description = "counts its own calls"
    args_schema = _Args

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: _Args) -> ToolOutcome:
        self.calls += 1
        return ToolOutcome(
            success=True, summary=f"found 3 events for {args.ticker}", data={"events": [1, 2, 3]}
        )


def test_an_adapter_calls_the_underlying_service_exactly_once():
    tool = _CountingTool()
    adapted = as_langchain_tool(tool)

    content, artifact = adapted.func(ticker="MU")  # type: ignore[misc]

    assert tool.calls == 1
    assert content == "found 3 events for MU"
    assert artifact.data == {"events": [1, 2, 3]}


def test_the_adapter_preserves_name_description_and_schema():
    tool = _CountingTool()
    adapted = as_langchain_tool(tool)
    assert adapted.name == tool.name
    assert adapted.description == tool.description
    assert adapted.args_schema is _Args


def test_every_registered_tool_is_classified(db_session):
    registry = build_tool_registry(db_session, _StubEmbedder())
    assert set(registry) == set(TOOL_ACCESS), (
        "a tool with no access classification must not reach the graph"
    )
    assert set(registry) == set(TOOL_EVIDENCE_CATEGORY)


def test_no_registered_tool_writes_to_the_database(db_session):
    """The READ_ONLY claim in TOOL_ACCESS, checked against the source."""
    import inspect

    registry = build_tool_registry(db_session, _StubEmbedder())
    for name, tool in registry.items():
        source = inspect.getsource(type(tool))
        for mutation in (".add(", ".commit(", ".flush(", ".delete("):
            assert mutation not in source, f"{name} appears to mutate state via {mutation}"
        assert access_of(name) in (ToolAccess.READ_ONLY, ToolAccess.DERIVED_CALCULATION)


def test_an_unclassified_tool_is_refused():
    with pytest.raises(ExecutionToolRefused, match="no access classification"):
        access_of("some_new_tool")


@pytest.mark.parametrize(
    "name",
    [
        "place_order",
        "submit_order_for_candidate",
        "modify_order",
        "cancel_order",
        "exercise_option",
        "execute_trade_dry_run",
        "submit_bracket_order",
    ],
)
def test_a_brokerage_execution_tool_cannot_be_adapted(name):
    """Requirement 16, enforced by shape rather than by a list of exact
    names -- including the well-meaning "dry run" variant."""

    class _Exec(Tool[_Args]):
        description = "d"
        args_schema = _Args

        def run(self, args: _Args) -> ToolOutcome:  # pragma: no cover
            raise AssertionError("must never be reachable")

    _Exec.name = name
    with pytest.raises(ExecutionToolRefused, match="brokerage execution"):
        as_langchain_tool(_Exec())


def test_write_tools_are_not_exposed_to_the_graph(db_session, monkeypatch):
    monkeypatch.setitem(TOOL_ACCESS, "get_options_snapshot", ToolAccess.WRITE)
    adapted, registry = build_langchain_tools(db_session, _StubEmbedder())
    assert "get_options_snapshot" in registry, "the underlying registry is unchanged"
    assert "get_options_snapshot" not in tool_names(adapted)


def test_the_graph_sees_all_seven_read_only_tools(db_session):
    adapted, _ = build_langchain_tools(db_session, _StubEmbedder())
    assert sorted(tool_names(adapted)) == [
        "calculate_implied_move",
        "calculate_strategy_payoff",
        "compare_guidance",
        "get_analyst_estimates",
        "get_historical_earnings",
        "get_options_snapshot",
        "search_filings",
    ]


# --- retriever adapter --------------------------------------------------


def test_the_retrieval_scope_maps_as_of_onto_the_filing_date_bound():
    from datetime import date

    filters = RetrievalScope(company_ids=[7], as_of=date(2026, 6, 30)).to_filters()
    assert filters is not None
    assert filters.company_ids == [7]
    assert filters.filing_date_to == date(2026, 6, 30)


def test_an_unscoped_retrieval_produces_no_filters():
    assert RetrievalScope().to_filters() is None


def test_a_document_carries_every_field_a_citation_is_built_from():
    from datetime import date

    chunk = RetrievedChunk(
        chunk_id=1,
        filing_id=2,
        company_id=3,
        ticker="MU",
        filing_type="10-K",
        filing_date=date(2026, 1, 15),
        source_url="https://sec.gov/x",
        section="Item 1A",
        chunk_index=4,
        text="risk factors",
        score=0.91,
        accession_number="0001-26-000001",
    )
    document = chunk_to_document(chunk)
    assert document.page_content == "risk factors"
    for field in (
        "ticker",
        "filing_type",
        "filing_date",
        "section",
        "source_url",
        "accession_number",
    ):
        assert field in document.metadata, f"{field} is needed to build a Citation"
    assert document.metadata["score"] == 0.91


def test_the_retriever_delegates_to_hybrid_search_and_adds_no_ranking(db_session, monkeypatch):
    """Requirement 19: no generic vector-only path sneaks in."""
    seen = {}

    def _fake_hybrid(db, query_text, query_embedding, filters, k=10, **kwargs):
        seen["query"] = query_text
        seen["k"] = k
        seen["filters"] = filters
        return []

    monkeypatch.setattr("agents.adapters.retriever.hybrid_search", _fake_hybrid)
    retriever = EDLHybridRetriever(
        db=db_session, embedder=_StubEmbedder(), k=5, scope=RetrievalScope(company_ids=[3])
    )

    assert retriever.invoke("margin trend") == []
    assert seen["query"] == "margin trend"
    assert seen["k"] == 5
    assert seen["filters"].company_ids == [3]
