"""Retrieval-only evaluation: for each labeled (query, ticker, relevant_chunk_ids)
item, runs the same hybrid_search used by search_filings / /research/documents
(scoped to the item's ticker, mirroring how those callers filter), and scores
Recall@3/5/10 and MRR against the hand-verified relevant set.

No LLM calls — pure retrieval quality, cheap to re-run.
"""

from _bootstrap import get_db_session, load_jsonl

from evaluation.metrics import mean_reciprocal_rank, recall_at_k  # noqa: E402
from evaluation.models import RetrievalItemResult, RetrievalSummary  # noqa: E402
from models.company import Company  # noqa: E402
from rag.embeddings import FastEmbedProvider  # noqa: E402
from rag.retrieval import RetrievalFilters, hybrid_search  # noqa: E402

K_MAX = 10


def run() -> RetrievalSummary:
    db = get_db_session()
    embedder = FastEmbedProvider()
    items = load_jsonl("retrieval_qa.jsonl")

    results: list[RetrievalItemResult] = []
    for item in items:
        company = db.query(Company).filter(Company.ticker == item["ticker"]).one()
        filters = RetrievalFilters(company_ids=[company.id])
        query_embedding = embedder.embed([item["query"]])[0]
        chunks = hybrid_search(db, item["query"], query_embedding, filters, k=K_MAX)
        retrieved_ids = [c.chunk_id for c in chunks]
        relevant = set(item["relevant_chunk_ids"])

        results.append(
            RetrievalItemResult(
                id=item["id"],
                query=item["query"],
                recall_at_3=recall_at_k(retrieved_ids, relevant, k=3),
                recall_at_5=recall_at_k(retrieved_ids, relevant, k=5),
                recall_at_10=recall_at_k(retrieved_ids, relevant, k=10),
                mrr=mean_reciprocal_rank(retrieved_ids, relevant),
                retrieved_count=len(retrieved_ids),
            )
        )

    db.close()
    n = len(results)
    return RetrievalSummary(
        item_count=n,
        mean_recall_at_3=sum(r.recall_at_3 for r in results) / n,
        mean_recall_at_5=sum(r.recall_at_5 for r in results) / n,
        mean_recall_at_10=sum(r.recall_at_10 for r in results) / n,
        mean_mrr=sum(r.mrr for r in results) / n,
        items=results,
    )


if __name__ == "__main__":
    summary = run()
    print(summary.model_dump_json(indent=2))
