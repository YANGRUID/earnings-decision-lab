from fastapi import APIRouter, Query, Request

from api.deps import DbSession, Embedder, Orchestrator
from api.exceptions import RateLimitedError
from models.company import Company
from rag.context import assemble_context
from rag.retrieval import RetrievalFilters, hybrid_search
from schemas.api import (
    CitationResponse,
    ExecutionTraceResponse,
    FilingSearchResponse,
    ResearchQueryRequest,
    ResearchQueryResponse,
    ToolCallResponse,
)

router = APIRouter(prefix="/research", tags=["research"])


@router.post("/query", response_model=ResearchQueryResponse)
def research_query(
    body: ResearchQueryRequest, request: Request, orchestrator: Orchestrator
) -> ResearchQueryResponse:
    if not request.app.state.research_rate_limiter.allow():
        raise RateLimitedError(
            "Too many research queries in a short window — each one runs several real LLM "
            "calls. Please wait a moment and try again."
        )

    result = orchestrator.run(body.question)
    trace = result.trace
    return ResearchQueryResponse(
        question=result.question,
        answer=result.answer,
        citations=[CitationResponse.from_citation(c) for c in result.citations],
        trace=ExecutionTraceResponse(
            intent_category=trace.intent_category,
            planning_method=trace.planning_method,
            tool_calls=[
                ToolCallResponse(
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                    success=tc.success,
                    duration_ms=tc.duration_ms,
                    summary=tc.summary,
                    error=tc.error,
                    query_description=tc.query_description,
                )
                for tc in trace.tool_calls
            ],
            verification_ran=trace.verification_ran,
            verification_supported=trace.verification_supported,
            revised=trace.revised,
            model=trace.model,
            total_input_tokens=trace.total_input_tokens,
            total_output_tokens=trace.total_output_tokens,
            estimated_cost_usd=trace.estimated_cost_usd,
            total_duration_ms=trace.total_duration_ms,
        ),
    )


@router.get("/documents", response_model=FilingSearchResponse)
def search_documents(
    db: DbSession,
    embedder: Embedder,
    query: str,
    ticker: str | None = None,
    k: int = Query(default=5, ge=1, le=15),
) -> FilingSearchResponse:
    filters = None
    if ticker:
        company = db.query(Company).filter(Company.ticker == ticker.upper()).one_or_none()
        filters = RetrievalFilters(company_ids=[company.id]) if company else RetrievalFilters(
            company_ids=[-1]
        )

    query_embedding = embedder.embed([query])[0]
    chunks = hybrid_search(db, query, query_embedding, filters, k=k)
    assembled = assemble_context(chunks)
    return FilingSearchResponse(
        context_text=assembled.context_text,
        citations=[CitationResponse.from_citation(c) for c in assembled.citations],
    )
