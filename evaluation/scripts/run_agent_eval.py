"""Full agent-orchestration evaluation: for each labeled item, runs the real
AgentOrchestrator (intent classification -> planning -> tool execution ->
verification), same as the /research/query endpoint, and checks:

  - intent_correct: did IntentClassification match the expected category?
  - tools_correct: did the tool calls include (at least) the expected
    tool(s)? An agent that also calls a reasonable extra tool still passes
    -- this checks necessary tool usage, not exact-set equality.

Latency, token usage, and estimated cost come straight from each run's
real ExecutionTrace (see agents/types.py); nothing here is estimated
separately. Makes several real, billed LLM calls per item (this is the
most expensive of the four eval categories) -- see docs/evaluation.md for
the measured cost.
"""

from _bootstrap import get_db_session, get_llm_and_embedder, load_jsonl

from agents.orchestrator import AgentOrchestrator
from evaluation.models import AgentItemResult, AgentSummary


def run() -> AgentSummary:
    db = get_db_session()
    llm, embedder = get_llm_and_embedder()
    orchestrator = AgentOrchestrator(db, llm, embedder)
    items = load_jsonl("agent_qa.jsonl")

    results: list[AgentItemResult] = []
    for item in items:
        response = orchestrator.run(item["query"])
        trace = response.trace
        actual_tools = [tc.tool_name for tc in trace.tool_calls] if trace else []
        expected_tools = item["expected_tools"]
        tools_correct = all(t in actual_tools for t in expected_tools) and (
            bool(expected_tools) or not actual_tools
        )

        results.append(
            AgentItemResult(
                id=item["id"],
                query=item["query"],
                expected_intent=item["expected_intent"],
                actual_intent=trace.intent_category if trace else "unknown",
                intent_correct=(trace.intent_category == item["expected_intent"])
                if trace
                else False,
                expected_tools=expected_tools,
                actual_tools=actual_tools,
                tools_correct=tools_correct,
                verification_ran=trace.verification_ran if trace else False,
                verification_supported=trace.verification_supported if trace else None,
                total_duration_ms=trace.total_duration_ms if trace else 0.0,
                total_input_tokens=trace.total_input_tokens if trace else 0,
                total_output_tokens=trace.total_output_tokens if trace else 0,
                estimated_cost_usd=float(trace.estimated_cost_usd)
                if trace and trace.estimated_cost_usd is not None
                else None,
            )
        )

    db.close()
    n = len(results)
    return AgentSummary(
        item_count=n,
        intent_accuracy=sum(1 for r in results if r.intent_correct) / n,
        tool_selection_accuracy=sum(1 for r in results if r.tools_correct) / n,
        verification_run_rate=sum(1 for r in results if r.verification_ran) / n,
        mean_duration_ms=sum(r.total_duration_ms for r in results) / n,
        total_estimated_cost_usd=sum(r.estimated_cost_usd or 0.0 for r in results),
        items=results,
    )


if __name__ == "__main__":
    summary = run()
    print(summary.model_dump_json(indent=2))
