"""Phase 4 RAG hardening (2026-08-26), Section 25 -- never instantiates
the real FastEmbedReranker (would need a real network download, same
convention as FastEmbedProvider -- see test_api_startup_resilience.py's
own stubbing pattern)."""

from datetime import date

from rag.reranking import MAX_RERANK_CANDIDATES, Reranker, rerank_chunks
from rag.retrieval import RetrievedChunk


def _chunk(chunk_id: int, text: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        filing_id=1,
        company_id=1,
        ticker="ZZRAG",
        filing_type="10-K",
        filing_date=date(2026, 1, 1),
        source_url="https://example.com",
        section="Item 1A",
        chunk_index=0,
        text=text,
        score=score,
    )


class _ReverseOrderReranker(Reranker):
    """A fake that deterministically reverses the input order -- proves
    rerank_chunks actually re-sorts by the reranker's own scores, not
    just passing hybrid_search's scores through."""

    model_name = "fake-reverse"
    version = "test-v1"

    def score(self, query: str, documents: list[str]) -> list[float]:
        return [float(i) for i in range(len(documents))]


class _RaisingReranker(Reranker):
    model_name = "fake-raising"
    version = "test-v1"

    def score(self, query: str, documents: list[str]) -> list[float]:
        raise RuntimeError("real model-loading failure")


def test_rerank_reverses_order_when_scores_favor_the_last_candidate():
    chunks = [_chunk(1, "first", 0.9), _chunk(2, "second", 0.8), _chunk(3, "third", 0.7)]

    result = rerank_chunks(_ReverseOrderReranker(), "query", chunks)

    assert [c.chunk_id for c in result] == [3, 2, 1]


def test_rerank_bounds_to_top_n_leaving_the_tail_untouched():
    chunks = [_chunk(i, f"chunk {i}", 1.0 / i) for i in range(1, 6)]

    result = rerank_chunks(_ReverseOrderReranker(), "query", chunks, top_n=3)

    # Only the first 3 were reranked (and reversed); the last 2 are
    # untouched, in their original hybrid_search order.
    assert [c.chunk_id for c in result[:3]] == [3, 2, 1]
    assert [c.chunk_id for c in result[3:]] == [4, 5]


def test_rerank_fails_safe_returning_original_order_unchanged():
    chunks = [_chunk(1, "first", 0.9), _chunk(2, "second", 0.8)]

    result = rerank_chunks(_RaisingReranker(), "query", chunks)

    assert result == chunks


def test_rerank_empty_list_is_a_noop():
    assert rerank_chunks(_ReverseOrderReranker(), "query", []) == []


def test_max_rerank_candidates_is_a_real_bound():
    assert MAX_RERANK_CANDIDATES > 0
    assert MAX_RERANK_CANDIDATES <= 50  # a real per-request latency bound, not unbounded
