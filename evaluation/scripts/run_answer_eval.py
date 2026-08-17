"""End-to-end RAG-answer evaluation: for each labeled item, runs the real
rag.answer.answer_question pipeline (hybrid retrieval -> context assembly ->
one live LLM call) against the configured provider, then scores:

  - fact_coverage: deterministic substring check of required_facts in the
    generated answer text (see evaluation.metrics.fact_coverage for the
    known false-negative tradeoff of this approach vs. an LLM judge).
  - citation_precision / citation_completeness: against the hand-verified
    relevant_chunk_ids.

rag.context.Citation (what answer_question returns) is a UI-facing shape
keyed by (ticker, filing_date, section), not a raw chunk id, and a section
commonly spans many chunks in one filing -- reversing a Citation back to
one chunk id would be ambiguous. Instead this script separately calls the
same hybrid_search with the same (query, filters, k=6 default) that
answer_question uses internally to recover the actual chunk ids retrieved.
Retrieval is deterministic (no LLM involved), so this reproduces exactly
what the pipeline saw, at no extra LLM cost.

One dataset item ("ans-none-01") is a deliberate negative control with no
relevant_chunk_ids/required_facts -- it is scored separately for honest
abstention rather than folded into the fact_coverage/citation numbers.

Makes real, billed LLM calls. See docs/evaluation.md for the measured cost.
"""

import time

from _bootstrap import get_db_session, get_llm_and_embedder, load_jsonl

from evaluation.metrics import citation_completeness, citation_precision, fact_coverage
from evaluation.models import RagAnswerItemResult, RagAnswerSummary
from models.company import Company
from rag.answer import answer_question
from rag.retrieval import RetrievalFilters, hybrid_search

ABSTENTION_PHRASES = (
    "no matching filing content",
    "does not state",
    "does not contain",
    "does not mention",
    "not stated",
    "no information",
    "not mentioned",
    "not disclosed",
    "unable to find",
    "could not find",
    "no mention of",
    "cannot answer",
    "can't answer",
    "i cannot",
    "there is no",
)


def run() -> RagAnswerSummary:
    db = get_db_session()
    llm, embedder = get_llm_and_embedder()
    items = load_jsonl("rag_answer_qa.jsonl")

    results: list[RagAnswerItemResult] = []
    for item in items:
        company = db.query(Company).filter(Company.ticker == item["ticker"]).one()
        filters = RetrievalFilters(company_ids=[company.id])

        query_embedding = embedder.embed([item["query"]])[0]
        retrieved_chunk_ids = [
            c.chunk_id for c in hybrid_search(db, item["query"], query_embedding, filters, k=6)
        ]

        start = time.monotonic()
        answered = answer_question(db, llm, embedder, item["query"], filters=filters)
        duration_ms = (time.monotonic() - start) * 1000

        relevant = set(item["relevant_chunk_ids"])
        is_negative_control = not relevant and not item["required_facts"]

        if is_negative_control:
            lowered = answered.answer.lower()
            abstained = any(phrase in lowered for phrase in ABSTENTION_PHRASES)
            results.append(
                RagAnswerItemResult(
                    id=item["id"],
                    query=item["query"],
                    fact_coverage=1.0 if abstained else 0.0,
                    missing_facts=[] if abstained else ["honest-abstention-phrase"],
                    citation_precision=0.0,
                    citation_completeness=0.0,
                    retrieved_chunk_count=answered.retrieved_chunk_count,
                    answer_had_no_evidence=answered.retrieved_chunk_count == 0,
                    duration_ms=duration_ms,
                )
            )
            continue

        coverage, missing = fact_coverage(answered.answer, item["required_facts"])
        results.append(
            RagAnswerItemResult(
                id=item["id"],
                query=item["query"],
                fact_coverage=coverage,
                missing_facts=missing,
                citation_precision=citation_precision(retrieved_chunk_ids, relevant),
                citation_completeness=citation_completeness(retrieved_chunk_ids, relevant),
                retrieved_chunk_count=answered.retrieved_chunk_count,
                answer_had_no_evidence=answered.retrieved_chunk_count == 0,
                duration_ms=duration_ms,
            )
        )

    db.close()
    n = len(results)
    return RagAnswerSummary(
        item_count=n,
        mean_fact_coverage=sum(r.fact_coverage for r in results) / n,
        fully_correct_count=sum(1 for r in results if r.fact_coverage == 1.0),
        mean_citation_precision=sum(r.citation_precision for r in results) / n,
        mean_citation_completeness=sum(r.citation_completeness for r in results) / n,
        mean_duration_ms=sum(r.duration_ms for r in results) / n,
        items=results,
    )


if __name__ == "__main__":
    summary = run()
    print(summary.model_dump_json(indent=2))
