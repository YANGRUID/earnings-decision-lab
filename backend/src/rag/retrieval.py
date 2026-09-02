"""Hybrid retrieval: pgvector cosine similarity + PostgreSQL full-text
search, combined by Reciprocal Rank Fusion (RRF).

RRF-only remains the DEFAULT (no ``reranker`` passed) -- see
docs/ai_architecture.md for why that was the original deliberate choice at
this project's scale: RRF over two independently-reasonable rankings is a
well-established, much cheaper technique that doesn't require an
additional model dependency. Phase 4 RAG hardening (2026-08-26), Section
25 adds an OPTIONAL local cross-encoder reranker (rag/reranking.py) a
caller can opt into via the ``reranker`` parameter below, without changing
this function's existing behavior for every caller that doesn't pass one.
"""

import logging
import time
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.company import Company
from models.document_chunk import DocumentChunk

if TYPE_CHECKING:
    from rag.reranking import Reranker
from models.filing import Filing

log = logging.getLogger("rag.retrieval")

DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class RetrievalFilters:
    company_ids: list[int] | None = None
    filing_types: list[str] | None = None
    filing_date_from: date | None = None
    filing_date_to: date | None = None


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    filing_id: int
    company_id: int
    ticker: str
    filing_type: str
    filing_date: date
    source_url: str
    section: str | None
    chunk_index: int
    text: str
    score: float
    # Phase 4 AI Research source-transparency hardening (2026-08-26),
    # Section 28 -- the real SEC accession number, when the filing has
    # one (Filing.accession_number is nullable for a non-SEC filing type
    # in principle, though every real row today has one). Defaulted so
    # every existing test fixture that builds a RetrievedChunk directly
    # (never caring about this field) keeps working unchanged.
    accession_number: str | None = None


def _base_query(filters: RetrievalFilters | None):
    stmt = (
        select(DocumentChunk, Filing, Company)
        .join(Filing, DocumentChunk.filing_id == Filing.id)
        .join(Company, DocumentChunk.company_id == Company.id)
    )
    if filters is None:
        return stmt
    if filters.company_ids:
        stmt = stmt.where(DocumentChunk.company_id.in_(filters.company_ids))
    if filters.filing_types:
        stmt = stmt.where(Filing.filing_type.in_(filters.filing_types))
    if filters.filing_date_from:
        stmt = stmt.where(Filing.filing_date >= filters.filing_date_from)
    if filters.filing_date_to:
        stmt = stmt.where(Filing.filing_date <= filters.filing_date_to)
    return stmt


def _to_retrieved_chunk(
    chunk: DocumentChunk, filing: Filing, company: Company, score: float
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk.id,
        filing_id=filing.id,
        company_id=company.id,
        ticker=company.ticker,
        filing_type=filing.filing_type.value
        if hasattr(filing.filing_type, "value")
        else filing.filing_type,
        filing_date=filing.filing_date,
        source_url=filing.source_url,
        accession_number=filing.accession_number,
        section=chunk.section,
        chunk_index=chunk.chunk_index,
        text=chunk.text,
        score=score,
    )


def vector_search(
    db: Session,
    query_embedding: list[float],
    filters: RetrievalFilters | None = None,
    k: int = 10,
) -> list[RetrievedChunk]:
    distance = DocumentChunk.embedding.cosine_distance(query_embedding)
    stmt = _base_query(filters).add_columns(distance.label("distance"))
    stmt = stmt.order_by(distance).limit(k)
    rows = db.execute(stmt).all()
    return [
        _to_retrieved_chunk(chunk, filing, company, score=1 - dist)
        for chunk, filing, company, dist in rows
    ]


def keyword_search(
    db: Session,
    query_text: str,
    filters: RetrievalFilters | None = None,
    k: int = 10,
) -> list[RetrievedChunk]:
    ts_vector = func.to_tsvector("english", DocumentChunk.text)
    ts_query = func.plainto_tsquery("english", query_text)
    rank_expr = func.ts_rank(ts_vector, ts_query)

    stmt = (
        _base_query(filters).where(ts_vector.op("@@")(ts_query)).order_by(rank_expr.desc()).limit(k)
    )
    rows = db.execute(stmt).all()
    return [
        _to_retrieved_chunk(chunk, filing, company, score=0.0) for chunk, filing, company in rows
    ]


def hybrid_search(
    db: Session,
    query_text: str,
    query_embedding: list[float],
    filters: RetrievalFilters | None = None,
    k: int = 10,
    rrf_k: int = DEFAULT_RRF_K,
    reranker: "Reranker | None" = None,
) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion: score(chunk) = sum(1 / (rrf_k + rank + 1))
    across whichever of the two ranked lists it appears in. A chunk found by
    both searches outranks one found by only one, without needing the two
    searches' raw scores (cosine similarity and ts_rank aren't comparable)
    to be normalized against each other.

    ``reranker`` (Phase 4 RAG hardening, 2026-08-26, Section 25) --
    OPTIONAL, deliberately not constructed here: a caller passes a real
    rag.reranking.Reranker instance to opt in. None (the default, every
    real caller today) preserves RRF-only ranking exactly, unchanged.
    """
    start = time.monotonic()
    vector_results = vector_search(db, query_embedding, filters, k=k * 2)
    keyword_results = keyword_search(db, query_text, filters, k=k * 2)

    rrf_scores: dict[int, float] = {}
    by_id: dict[int, RetrievedChunk] = {}
    for ranked_list in (vector_results, keyword_results):
        for rank, chunk in enumerate(ranked_list):
            rrf_scores[chunk.chunk_id] = rrf_scores.get(chunk.chunk_id, 0.0) + 1.0 / (
                rrf_k + rank + 1
            )
            by_id[chunk.chunk_id] = chunk

    ranked_ids = sorted(rrf_scores, key=lambda cid: rrf_scores[cid], reverse=True)[:k]
    results = [replace(by_id[cid], score=rrf_scores[cid]) for cid in ranked_ids]

    if reranker is not None:
        from rag.reranking import rerank_chunks

        results = rerank_chunks(reranker, query_text, results)

    log.info(
        "hybrid search completed",
        extra={
            "duration_ms": round((time.monotonic() - start) * 1000, 2),
            "vector_hits": len(vector_results),
            "keyword_hits": len(keyword_results),
            "returned": len(results),
            "k": k,
        },
    )
    return results
