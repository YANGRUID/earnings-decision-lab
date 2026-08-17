import logging

from fastapi import APIRouter, BackgroundTasks, Query, Request

from api.deps import DbSession, Embedder, Orchestrator
from api.exceptions import NotFoundError, RateLimitedError
from core.config import get_settings
from db.session import SessionLocal
from models.company import Company
from models.document_chunk import DocumentChunk
from models.earnings_event import EarningsEvent
from models.filing import Filing
from models.price_bar import PriceBar
from providers.sec_edgar import SECEdgarProvider
from rag.context import assemble_context
from rag.embeddings import EmbeddingProvider
from rag.retrieval import RetrievalFilters, hybrid_search
from schemas.api import (
    CitationResponse,
    CompanyResponse,
    EarningsEstimateResponse,
    ExecutionTraceResponse,
    FilingSearchResponse,
    ResearchJobQueuedResponse,
    ResearchJobResponse,
    ResearchOverviewResponse,
    ResearchQueryRequest,
    ResearchQueryResponse,
    ToolCallResponse,
    VolatilitySnapshotResponse,
)
from services.market_expectations import get_latest_earnings_estimate
from services.options_analytics import get_latest_volatility_snapshot
from services.research_orchestration import (
    UnsupportedSymbolError,
    build_research_providers,
    get_latest_research_job,
    get_running_research_job,
    prepare_company_research,
)
from services.symbol_resolution import normalize_ticker, resolve_symbol

log = logging.getLogger("api.research")

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
        filters = (
            RetrievalFilters(company_ids=[company.id])
            if company
            else RetrievalFilters(company_ids=[-1])
        )

    query_embedding = embedder.embed([query])[0]
    chunks = hybrid_search(db, query, query_embedding, filters, k=k)
    assembled = assemble_context(chunks)
    return FilingSearchResponse(
        context_text=assembled.context_text,
        citations=[CitationResponse.from_citation(c) for c in assembled.citations],
    )


def _run_preparation_background(ticker: str, force: bool, embedder: EmbeddingProvider) -> None:
    """Runs the full preparation pipeline outside the request/response
    cycle, on its own fresh DB session -- the request's own session
    (api.deps.get_db) closes the moment the response is sent, well before
    this has a chance to run.
    """
    db = SessionLocal()
    try:
        providers = build_research_providers(get_settings(), embedder)
        prepare_company_research(db, ticker, providers, force=force)
    except UnsupportedSymbolError:
        # Already validated synchronously in _kickoff before this was ever
        # scheduled -- reaching this would mean the ticker became
        # unsupported between those two points, which doesn't happen in
        # practice (SEC's ticker list doesn't shrink mid-request). Logged,
        # not raised: there's no HTTP response left to attach it to.
        log.warning("%s: became unsupported between validation and background run", ticker)
    except Exception:
        log.exception("%s: background research preparation crashed", ticker)
    finally:
        db.close()


def _kickoff(
    symbol: str,
    background_tasks: BackgroundTasks,
    db: DbSession,
    embedder: EmbeddingProvider,
    force: bool,
) -> ResearchJobResponse | ResearchJobQueuedResponse:
    settings = get_settings()
    edgar = SECEdgarProvider(user_agent=settings.sec_edgar_user_agent)
    resolution = resolve_symbol(db, edgar, symbol)
    if not resolution.supported:
        raise UnsupportedSymbolError(resolution.reason or f"{symbol!r} is not supported")

    # Idempotency: a request that arrives while one is already running for
    # this ticker reuses it instead of scheduling a duplicate background
    # task that would just race the first one over the same rows.
    running = get_running_research_job(db, resolution.ticker)
    if running is not None:
        return ResearchJobResponse.model_validate(running)

    background_tasks.add_task(_run_preparation_background, resolution.ticker, force, embedder)
    return ResearchJobQueuedResponse(ticker=resolution.ticker)


@router.post("/{symbol}/prepare", response_model=ResearchJobResponse | ResearchJobQueuedResponse)
def prepare_research(
    symbol: str, background_tasks: BackgroundTasks, db: DbSession, embedder: Embedder
) -> ResearchJobResponse | ResearchJobQueuedResponse:
    return _kickoff(symbol, background_tasks, db, embedder, force=False)


@router.post("/{symbol}/refresh", response_model=ResearchJobResponse | ResearchJobQueuedResponse)
def refresh_research(
    symbol: str, background_tasks: BackgroundTasks, db: DbSession, embedder: Embedder
) -> ResearchJobResponse | ResearchJobQueuedResponse:
    return _kickoff(symbol, background_tasks, db, embedder, force=True)


@router.get("/{symbol}/status", response_model=ResearchJobResponse)
def research_status(symbol: str, db: DbSession):
    ticker = normalize_ticker(symbol)
    job = get_latest_research_job(db, ticker)
    if job is None:
        raise NotFoundError(
            f"no research preparation job found for {ticker!r} yet -- call "
            f"POST /research/{ticker}/prepare first"
        )
    return job


@router.get("/{symbol}/overview", response_model=ResearchOverviewResponse)
def research_overview(symbol: str, db: DbSession) -> ResearchOverviewResponse:
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    latest_job = get_latest_research_job(db, ticker)
    latest_job_response = ResearchJobResponse.model_validate(latest_job) if latest_job else None

    if company is None:
        return ResearchOverviewResponse(
            ticker=ticker,
            company=None,
            latest_job=latest_job_response,
            earnings_events_count=0,
            price_bars_count=0,
            filings_count=0,
            filing_chunks_count=0,
            latest_earnings_estimate=None,
            latest_volatility_snapshot=None,
        )

    latest_estimate = get_latest_earnings_estimate(db, company.id)
    latest_volatility = get_latest_volatility_snapshot(db, company.id)

    return ResearchOverviewResponse(
        ticker=ticker,
        company=CompanyResponse.model_validate(company),
        latest_job=latest_job_response,
        earnings_events_count=(
            db.query(EarningsEvent).filter(EarningsEvent.company_id == company.id).count()
        ),
        price_bars_count=db.query(PriceBar).filter(PriceBar.ticker == ticker).count(),
        filings_count=db.query(Filing).filter(Filing.company_id == company.id).count(),
        filing_chunks_count=(
            db.query(DocumentChunk).filter(DocumentChunk.company_id == company.id).count()
        ),
        latest_earnings_estimate=(
            EarningsEstimateResponse.model_validate(latest_estimate) if latest_estimate else None
        ),
        latest_volatility_snapshot=(
            VolatilitySnapshotResponse.model_validate(latest_volatility)
            if latest_volatility
            else None
        ),
    )
