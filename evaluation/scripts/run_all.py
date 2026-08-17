"""Runs all four evaluation categories against the currently configured LLM
provider and writes both a timestamped result file and results/latest.json.
Result JSON files are gitignored (see evaluation/results/.gitkeep) -- the
measured numbers that matter get written into docs/evaluation.md by hand
after a real run, not silently regenerated on every commit.

    cd backend && uv run python ../evaluation/scripts/run_all.py
"""

from datetime import UTC, datetime

from _bootstrap import RESULTS_DIR, get_llm_and_embedder

import run_agent_eval
import run_answer_eval
import run_extraction_eval
import run_retrieval_eval
from core.config import get_settings
from evaluation.models import EvaluationRun


def run() -> EvaluationRun:
    settings = get_settings()
    llm, embedder = get_llm_and_embedder()

    print("Running retrieval eval (no LLM calls)...")
    retrieval = run_retrieval_eval.run()
    print(f"  mean_recall_at_5={retrieval.mean_recall_at_5:.3f} mean_mrr={retrieval.mean_mrr:.3f}")

    print("Running RAG-answer eval (live LLM calls)...")
    rag_answer = run_answer_eval.run()
    print(f"  mean_fact_coverage={rag_answer.mean_fact_coverage:.3f}")

    print("Running agent-orchestration eval (live LLM calls)...")
    agent = run_agent_eval.run()
    print(
        f"  intent_accuracy={agent.intent_accuracy:.3f} "
        f"tool_accuracy={agent.tool_selection_accuracy:.3f}"
    )

    print("Running structured-extraction eval (live LLM calls)...")
    extraction = run_extraction_eval.run()
    print(
        f"  non_capex_null_accuracy={extraction.non_capex_null_accuracy:.3f} "
        f"capex_accuracy={extraction.capex_accuracy:.3f}"
    )

    return EvaluationRun(
        run_at=datetime.now(UTC),
        llm_provider=settings.llm_provider,
        llm_model=llm.model,
        embedding_model=embedder.model_name,
        retrieval=retrieval,
        rag_answer=rag_answer,
        agent=agent,
        extraction=extraction,
    )


if __name__ == "__main__":
    result = run()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = result.run_at.strftime("%Y%m%dT%H%M%SZ")
    run_path = RESULTS_DIR / f"eval_run_{timestamp}.json"
    latest_path = RESULTS_DIR / "latest.json"
    payload = result.model_dump_json(indent=2)
    run_path.write_text(payload)
    latest_path.write_text(payload)
    print(f"\nWrote {run_path}")
    print(f"Wrote {latest_path}")
