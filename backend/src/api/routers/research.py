import logging

from fastapi import APIRouter, BackgroundTasks, Query, Request

from analytics.options.move_compatibility import assess_move_compatibility
from analytics.options.strategy_ranking import rank_strategy_candidates
from api.deps import LLM, DbSession, Embedder, Orchestrator
from api.exceptions import NotFoundError, RateLimitedError
from core.config import get_settings
from db.session import SessionLocal
from models.company import Company
from models.document_chunk import DocumentChunk
from models.earnings_event import EarningsEvent
from models.filing import Filing
from models.price_bar import PriceBar
from providers.sec_edgar import SECEdgarProvider
from providers.types import OptionQuote
from rag.context import assemble_context
from rag.embeddings import EmbeddingProvider
from rag.retrieval import RetrievalFilters, hybrid_search
from schemas.api import (
    CitationResponse,
    CompanyResponse,
    EarningsEstimateResponse,
    EarningsThesisResponse,
    ExecutionTraceResponse,
    FilingSearchResponse,
    MoveCompatibilityResponse,
    OptionLegResponse,
    OptionQuoteResponse,
    RankedStrategyResponse,
    ResearchJobQueuedResponse,
    ResearchJobResponse,
    ResearchOverviewResponse,
    ResearchQueryRequest,
    ResearchQueryResponse,
    ScenarioPnlResponse,
    StrategyAnalysisResponse,
    StrategyLabResponse,
    ToolCallResponse,
    VolatilitySnapshotResponse,
)
from services.earnings_thesis import ThesisGenerationError, generate_earnings_thesis
from services.historical_moves import get_historical_move_pcts
from services.llm.errors import LLMError
from services.market_expectations import get_latest_earnings_estimate
from services.options_analytics import get_latest_options_chain, get_latest_volatility_snapshot
from services.research_orchestration import (
    UnsupportedSymbolError,
    build_research_providers,
    get_latest_research_job,
    get_running_research_job,
    prepare_company_research,
)
from services.strategy_generation import generate_strategy_candidates
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


def _option_quote_response(quote: OptionQuote) -> OptionQuoteResponse:
    return OptionQuoteResponse(
        expiration_date=quote.expiration_date,
        strike=quote.strike,
        option_type=quote.option_type,
        bid=quote.bid,
        ask=quote.ask,
        last_price=quote.last_price,
        volume=quote.volume,
        open_interest=quote.open_interest,
        implied_volatility=quote.implied_volatility,
        delta=quote.delta,
        gamma=quote.gamma,
        theta=quote.theta,
        vega=quote.vega,
        market_data_quality=quote.market_data_quality,
        source_provider=quote.source_provider,
    )


@router.get("/{symbol}/strategies", response_model=StrategyLabResponse)
def get_strategy_lab(symbol: str, db: DbSession) -> StrategyLabResponse:
    """Real, ranked strategy candidates for ``symbol``'s upcoming earnings,
    built entirely from the most recently ingested real options-chain
    snapshot -- see services/strategy_generation.py and
    analytics/options/strategy_ranking.py. Honestly empty (never
    fabricated) when no such snapshot exists yet, regardless of whether
    that's because the company hasn't been researched or because no
    options provider has real data for it.
    """
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    chain_response = [_option_quote_response(q) for q in get_latest_options_chain(db, company)]
    volatility = get_latest_volatility_snapshot(db, company.id)

    if volatility is None or volatility.target_earnings_date is None:
        return StrategyLabResponse(
            ticker=ticker,
            expiration=None,
            underlying_price=None,
            implied_move_pct=None,
            strategies=[],
            chain=chain_response,
        )

    candidates = generate_strategy_candidates(db, company, volatility.target_earnings_date)
    ranked = rank_strategy_candidates(candidates, ticker, volatility.implied_move_pct)
    historical_moves = get_historical_move_pcts(db, company.id)

    strategies = []
    for r in ranked:
        compatibility = assess_move_compatibility(r.candidate, historical_moves)
        strategies.append(
            RankedStrategyResponse(
                rank=r.rank,
                category=r.candidate.category.value,
                legs=[
                    OptionLegResponse(
                        option_type=leg.option_type.value,
                        action=leg.action.value,
                        strike=leg.strike,
                        premium=leg.premium,
                        quantity=leg.quantity,
                    )
                    for leg in r.candidate.legs
                ],
                analysis=StrategyAnalysisResponse(
                    net_premium=r.candidate.analysis.net_premium,
                    max_profit=r.candidate.analysis.max_profit,
                    max_loss=r.candidate.analysis.max_loss,
                    breakevens=list(r.candidate.analysis.breakevens),
                    return_on_risk=r.candidate.analysis.return_on_risk,
                ),
                score=r.score,
                explanation=r.explanation,
                scenario=ScenarioPnlResponse(
                    down_price=r.scenario.down_price,
                    down_pnl=r.scenario.down_pnl,
                    flat_pnl=r.scenario.flat_pnl,
                    up_price=r.scenario.up_price,
                    up_pnl=r.scenario.up_pnl,
                )
                if r.scenario
                else None,
                move_compatibility=MoveCompatibilityResponse(
                    method=compatibility.method,
                    sample_size=compatibility.sample_size,
                    requires_move_beyond_threshold=compatibility.requires_move_beyond_threshold,
                    required_move_pct=compatibility.required_move_pct,
                    compatible_count=compatibility.compatible_count,
                    compatible_pct=compatibility.compatible_pct,
                )
                if compatibility
                else None,
            )
        )

    return StrategyLabResponse(
        ticker=ticker,
        expiration=candidates[0].expiration if candidates else None,
        underlying_price=candidates[0].underlying_price if candidates else None,
        implied_move_pct=volatility.implied_move_pct,
        strategies=strategies,
        chain=chain_response,
    )


@router.post("/{symbol}/thesis", response_model=EarningsThesisResponse)
def get_earnings_thesis(
    symbol: str, request: Request, db: DbSession, llm: LLM, embedder: Embedder
) -> EarningsThesisResponse:
    if not request.app.state.research_rate_limiter.allow():
        raise RateLimitedError(
            "Too many AI requests in a short window — a thesis runs several real LLM calls. "
            "Please wait a moment and try again."
        )
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r} — prepare it first")

    try:
        result = generate_earnings_thesis(db, llm, embedder, company)
    except ThesisGenerationError as exc:
        raise LLMError(f"thesis generation failed: {exc}") from exc

    return EarningsThesisResponse(
        business_context=result.thesis.business_context,
        historical_earnings_pattern=result.thesis.historical_earnings_pattern,
        guidance_trend=result.thesis.guidance_trend,
        key_risks=result.thesis.key_risks,
        market_setup=result.thesis.market_setup,
        disclaimer=result.thesis.disclaimer,
        citations=[CitationResponse.from_citation(c) for c in result.citations],
        generated_at=result.generated_at,
        model=result.model,
    )
