"""Hybrid retrieval: pgvector cosine similarity + PostgreSQL full-text
search, combined by Reciprocal Rank Fusion (RRF).

No separate reranking model (e.g. a cross-encoder) is used — see
docs/ai_architecture.md for why that isn't justified yet at this project's
current scale (a handful of tickers, a modest real filing count): RRF over
two independently-reasonable rankings is a well-established, much cheaper
technique that doesn't require an additional model dependency, and can be
replaced by a real reranker later without changing this module's interface
if retrieval quality ever demonstrably needs it.
"""

from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.company import Company
from models.document_chunk import DocumentChunk
from models.filing import Filing

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
        _base_query(filters)
        .where(ts_vector.op("@@")(ts_query))
        .order_by(rank_expr.desc())
        .limit(k)
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
) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion: score(chunk) = sum(1 / (rrf_k + rank + 1))
    across whichever of the two ranked lists it appears in. A chunk found by
    both searches outranks one found by only one, without needing the two
    searches' raw scores (cosine similarity and ts_rank aren't comparable)
    to be normalized against each other.
    """
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
    return [replace(by_id[cid], score=rrf_scores[cid]) for cid in ranked_ids]
