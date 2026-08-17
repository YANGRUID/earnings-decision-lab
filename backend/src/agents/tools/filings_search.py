from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from models.company import Company
from rag.context import assemble_context
from rag.embeddings import EmbeddingProvider
from rag.retrieval import RetrievalFilters, hybrid_search


class FilingsSearchArgs(BaseModel):
    query: str = Field(description="Natural-language search query over filing text.")
    ticker: str | None = Field(default=None, description="Restrict to one covered ticker.")
    k: int = Field(default=5, ge=1, le=15)


class FilingsSearchTool(Tool):
    name = "search_filings"
    description = (
        "Hybrid (vector + keyword) search over real SEC filing text (10-K/10-Q/8-K) for "
        "NVDA, AMD, MU, SNDK. Returns cited excerpts, not a generated answer."
    )
    args_schema = FilingsSearchArgs

    def __init__(self, db: Session, embedder: EmbeddingProvider) -> None:
        self._db = db
        self._embedder = embedder

    def run(self, args: FilingsSearchArgs) -> ToolOutcome:
        filters = None
        if args.ticker:
            company = (
                self._db.query(Company).filter(Company.ticker == args.ticker.upper()).one_or_none()
            )
            if company is None:
                return ToolOutcome(
                    success=True,
                    summary=f"No covered company found for ticker {args.ticker!r}.",
                    data={"chunks": []},
                )
            filters = RetrievalFilters(company_ids=[company.id])

        query_embedding = self._embedder.embed([args.query])[0]
        chunks = hybrid_search(self._db, args.query, query_embedding, filters, k=args.k)
        assembled = assemble_context(chunks)

        return ToolOutcome(
            success=True,
            summary=f"Retrieved {len(chunks)} filing excerpts for query {args.query!r}.",
            data={"context_text": assembled.context_text},
            citations=assembled.citations,
        )
