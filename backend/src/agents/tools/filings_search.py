from datetime import date

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
    ticker: str | None = Field(
        default=None,
        description="Restrict to one company's filings -- always pass the real ticker the "
        "question is about, for any company, not only a fixed set.",
    )
    k: int = Field(default=5, ge=1, le=15)
    # Post-live correction (2026-08-25) Part A8 -- point-in-time safety:
    # when set, excludes any filing published after this cutoff (see
    # rag.retrieval.RetrievalFilters.filing_date_to, already built for
    # exactly this, just never threaded through from here before). None
    # (the default, every real caller today) means "as of now".
    as_of: date | None = Field(
        default=None,
        description="Optional point-in-time cutoff (YYYY-MM-DD) -- excludes filings after this "
        "date.",
    )


class FilingsSearchTool(Tool[FilingsSearchArgs]):
    name = "search_filings"
    description = (
        "Hybrid (vector + keyword) search over real SEC filing text (10-K/10-Q) for any "
        "company already research-ready in this system. Not limited to a fixed ticker list "
        "-- always pass the real ticker the question is about to keep results scoped to that "
        "one company. Returns cited excerpts, not a generated answer."
    )
    args_schema = FilingsSearchArgs

    def __init__(self, db: Session, embedder: EmbeddingProvider) -> None:
        self._db = db
        self._embedder = embedder

    def run(self, args: FilingsSearchArgs) -> ToolOutcome:
        filters = None
        if args.ticker or args.as_of:
            company_ids = None
            if args.ticker:
                company = (
                    self._db.query(Company)
                    .filter(Company.ticker == args.ticker.upper())
                    .one_or_none()
                )
                if company is None:
                    return ToolOutcome(
                        success=True,
                        summary=f"No covered company found for ticker {args.ticker!r}.",
                        data={"chunks": []},
                    )
                company_ids = [company.id]
            filters = RetrievalFilters(company_ids=company_ids, filing_date_to=args.as_of)

        query_embedding = self._embedder.embed([args.query])[0]
        chunks = hybrid_search(self._db, args.query, query_embedding, filters, k=args.k)
        assembled = assemble_context(chunks, evidence_cutoff=args.as_of)

        return ToolOutcome(
            success=True,
            summary=f"Retrieved {len(chunks)} filing excerpts for query {args.query!r}.",
            data={"context_text": assembled.context_text},
            citations=assembled.citations,
        )
