"""The research graph: nodes, edges, and two bounded conditional routes.

    START
      -> classify_intent
      -> window_context          (deterministic; no-op without an event)
      -> plan_research
      -> execute_tools
      -> merge_evidence
      -> evidence_quality_gate
           |- SUFFICIENT ............................-> synthesize
           |- PARTIAL/INSUFFICIENT and a round left -> targeted_retrieve
           |                                              -> merge_evidence
           |                                              -> evidence_quality_gate
           '- PARTIAL/INSUFFICIENT and none left ....-> synthesize
      -> verify
           |- supported, or unavailable .............-> END
           |- unsupported and a revision left .......-> revise -> verify
           '- unsupported and none left ..............-> END
      -> END

Both loops are bounded by a counter in the state, checked in the routing
function -- not by a recursion limit and not by a prompt asking the model
to stop (requirements 24, 25). ``MAX_RETRIEVAL_ROUNDS`` and
``MAX_REVISIONS`` live in agents/graph/state.py.

Two deliberate non-features:

- The gate going to ``synthesize`` on INSUFFICIENT is not a loophole. The
  evidence is still handed over, the quality verdict travels with it in
  state, and the caller reports the gap honestly -- which is what this
  project already does for a question whose every tool call failed. An
  insufficient-evidence run must still produce an honest "here is what is
  and is not on record", not an exception.
- There is no critic, no bull/bear pair and no judge (requirement 55).
  One controlled judgement plus one bounded verification is the methodology;
  adding adversarial roles would change it.
"""

from functools import partial
from typing import Final, Literal

from langgraph.graph import END, START, StateGraph

from agents.graph import nodes
from agents.graph.deps import GraphDeps
from agents.graph.state import MAX_RETRIEVAL_ROUNDS, MAX_REVISIONS, ResearchState
from schemas.agent import EvidenceQualityStatus

NODE_CLASSIFY_INTENT: Final = "classify_intent"
NODE_WINDOW_CONTEXT: Final = "window_context"
NODE_PLAN: Final = "plan_research"
NODE_EXECUTE_TOOLS: Final = "execute_tools"
NODE_MERGE_EVIDENCE: Final = "merge_evidence"
NODE_QUALITY_GATE: Final = "evidence_quality_gate"
NODE_TARGETED_RETRIEVE: Final = "targeted_retrieve"
NODE_SYNTHESIZE: Final = "synthesize"
NODE_VERIFY: Final = "verify"
NODE_REVISE: Final = "revise"


def route_after_quality_gate(
    state: ResearchState,
) -> Literal["targeted_retrieve", "synthesize"]:
    """Retry the gap once, then proceed with whatever is genuinely there."""
    quality = state.get("evidence_quality") or {}
    status = quality.get("status")
    if status == EvidenceQualityStatus.SUFFICIENT.value:
        return NODE_SYNTHESIZE
    if state.get("retrieval_attempt_count", 0) >= MAX_RETRIEVAL_ROUNDS:
        return NODE_SYNTHESIZE
    if not quality.get("recommended_retrieval"):
        # Nothing specific to retry -- e.g. the only problem is a conflict,
        # which re-fetching the same rows cannot resolve. Spending another
        # round here would buy the same contradiction twice.
        return NODE_SYNTHESIZE
    return NODE_TARGETED_RETRIEVE


def route_after_verify(state: ResearchState) -> Literal["revise", "__end__"]:
    verification = state.get("verification")
    if verification is None:
        # Verification did not run, or was unavailable. The legacy
        # orchestrator leaves the draft standing in exactly this case.
        return "__end__"
    if verification.get("supported"):
        return "__end__"
    if state.get("revision_count", 0) >= MAX_REVISIONS:
        return "__end__"
    return NODE_REVISE


def build_research_graph(deps: GraphDeps, *, checkpointer: object | None = None):
    """Compiles the graph with ``deps`` bound to every node.

    ``deps`` is per-request state (a DB session, a provider client, the
    loaded embedder), so the graph is compiled per request too. Compilation
    is cheap -- it builds an edge table, it does not load a model.
    """
    graph: StateGraph = StateGraph(ResearchState)

    graph.add_node(NODE_CLASSIFY_INTENT, partial(nodes.classify_intent, deps=deps))
    graph.add_node(NODE_WINDOW_CONTEXT, partial(nodes.window_context, deps=deps))
    graph.add_node(NODE_PLAN, partial(nodes.plan_research, deps=deps))
    graph.add_node(NODE_EXECUTE_TOOLS, partial(nodes.execute_tools, deps=deps))
    graph.add_node(NODE_MERGE_EVIDENCE, partial(nodes.merge_evidence, deps=deps))
    graph.add_node(NODE_QUALITY_GATE, partial(nodes.evidence_quality_gate, deps=deps))
    graph.add_node(NODE_TARGETED_RETRIEVE, partial(nodes.targeted_retrieve, deps=deps))
    graph.add_node(NODE_SYNTHESIZE, partial(nodes.synthesize, deps=deps))
    graph.add_node(NODE_VERIFY, partial(nodes.verify, deps=deps))
    graph.add_node(NODE_REVISE, partial(nodes.revise, deps=deps))

    graph.add_edge(START, NODE_CLASSIFY_INTENT)
    graph.add_edge(NODE_CLASSIFY_INTENT, NODE_WINDOW_CONTEXT)
    graph.add_edge(NODE_WINDOW_CONTEXT, NODE_PLAN)
    graph.add_edge(NODE_PLAN, NODE_EXECUTE_TOOLS)
    graph.add_edge(NODE_EXECUTE_TOOLS, NODE_MERGE_EVIDENCE)
    graph.add_edge(NODE_MERGE_EVIDENCE, NODE_QUALITY_GATE)
    graph.add_conditional_edges(
        NODE_QUALITY_GATE,
        route_after_quality_gate,
        {
            NODE_TARGETED_RETRIEVE: NODE_TARGETED_RETRIEVE,
            NODE_SYNTHESIZE: NODE_SYNTHESIZE,
        },
    )
    # The retrieval loop re-enters at merge_evidence, which recomputes the
    # blocks from the full record list -- so a second round adds evidence
    # instead of duplicating what round 0 already produced.
    graph.add_edge(NODE_TARGETED_RETRIEVE, NODE_MERGE_EVIDENCE)
    graph.add_edge(NODE_SYNTHESIZE, NODE_VERIFY)
    graph.add_conditional_edges(
        NODE_VERIFY, route_after_verify, {NODE_REVISE: NODE_REVISE, END: END}
    )
    graph.add_edge(NODE_REVISE, NODE_VERIFY)

    return graph.compile(checkpointer=checkpointer)  # type: ignore[arg-type]


def graph_shape() -> dict[str, object]:
    """The graph's declared shape, for the report and the diagnostics UI.

    Derived from the constants above rather than typed out again, so a
    documented bound cannot drift from the enforced one.
    """
    return {
        "nodes": [
            NODE_CLASSIFY_INTENT,
            NODE_WINDOW_CONTEXT,
            NODE_PLAN,
            NODE_EXECUTE_TOOLS,
            NODE_MERGE_EVIDENCE,
            NODE_QUALITY_GATE,
            NODE_TARGETED_RETRIEVE,
            NODE_SYNTHESIZE,
            NODE_VERIFY,
            NODE_REVISE,
        ],
        "conditional_routes": {
            NODE_QUALITY_GATE: [NODE_TARGETED_RETRIEVE, NODE_SYNTHESIZE],
            NODE_VERIFY: [NODE_REVISE, "end"],
        },
        "max_retrieval_rounds": MAX_RETRIEVAL_ROUNDS,
        "max_revisions": MAX_REVISIONS,
    }
