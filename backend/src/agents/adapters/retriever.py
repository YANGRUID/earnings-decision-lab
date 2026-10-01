"""LangChain ``BaseRetriever`` over this project's own hybrid retrieval.

Requirements 17-19, stated plainly: the retrieval behaviour stays ours.
This adapter does not chunk, embed, rank or fuse anything. It calls
``rag.retrieval.hybrid_search`` -- SEC-aware section chunks, pgvector
cosine search, Postgres full-text search, Reciprocal Rank Fusion, company
and as-of metadata filters -- and converts the ranked ``RetrievedChunk``
list into LangChain ``Document`` objects.

What this buys: the graph (and anything else LangChain-shaped later) can
accept a retriever interface. What it must never buy: a generic
``RecursiveCharacterTextSplitter`` plus vector-only search, which would
throw away section awareness, the keyword half of the hybrid, and the
structured citations the UI renders.

The ``Document.metadata`` carries exactly the fields
``rag.context.Citation`` is built from, so a citation produced through
this adapter is the same citation the existing code produces.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict
from sqlalchemy.orm import Session

from rag.embeddings import EmbeddingProvider
from rag.retrieval import RetrievalFilters, RetrievedChunk, hybrid_search


@dataclass(frozen=True)
class RetrievalScope:
    """The point-in-time and company scope a retrieval must honour.

    Separate from ``RetrievalFilters`` because ``as_of`` is a decision
    about evidence admissibility, not a SQL predicate: it maps onto
    ``filing_date_to``, and naming it ``as_of`` here keeps the graph's
    vocabulary honest about why the bound exists.
    """

    company_ids: list[int] | None = None
    as_of: date | None = None
    filing_types: list[str] | None = None

    def to_filters(self) -> RetrievalFilters | None:
        if self.company_ids is None and self.as_of is None and self.filing_types is None:
            return None
        return RetrievalFilters(
            company_ids=self.company_ids,
            filing_types=self.filing_types,
            filing_date_to=self.as_of,
        )


def chunk_to_document(chunk: RetrievedChunk) -> Document:
    return Document(
        page_content=chunk.text,
        metadata={
            "chunk_id": chunk.chunk_id,
            "filing_id": chunk.filing_id,
            "company_id": chunk.company_id,
            "ticker": chunk.ticker,
            "filing_type": chunk.filing_type,
            "filing_date": chunk.filing_date.isoformat(),
            "source_url": chunk.source_url,
            "section": chunk.section,
            "chunk_index": chunk.chunk_index,
            "accession_number": chunk.accession_number,
            "score": chunk.score,
        },
    )


class EDLHybridRetriever(BaseRetriever):
    """Delegates to ``hybrid_search``. Owns no retrieval logic of its own."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    db: Session
    embedder: EmbeddingProvider
    k: int = 5
    scope: RetrievalScope = RetrievalScope()

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun | None = None,
        **kwargs: Any,
    ) -> list[Document]:
        query_embedding = self.embedder.embed([query])[0]
        chunks = hybrid_search(
            self.db,
            query,
            query_embedding,
            self.scope.to_filters(),
            k=self.k,
        )
        return [chunk_to_document(c) for c in chunks]
