import logging
from datetime import UTC, datetime
from typing import TypedDict

from fastapi import APIRouter, BackgroundTasks, Query, Request

from analytics.market_session import get_market_session
from analytics.options.move_compatibility import assess_move_compatibility
from analytics.options.strategy_ranking import rank_strategy_candidates
from api.deps import LLM, DbSession, Embedder, Orchestrator
from api.exceptions import InvalidRequestError, NotFoundError, RateLimitedError
from core.config import get_settings
from db.session import SessionLocal
from models.ai_thesis_version import AIThesisVersion
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
    AIResearchHistoryItemResponse,
    AIThesisVersionResponse,
    CitationResponse,
    CompanyResponse,
    EarningsEstimateResponse,
    EarningsThesisResponse,
    ExecutionTraceResponse,
    FilingSearchResponse,
    HistoricalMoveStatsResponse,
    ManualEarningsDateRequest,
    MoveCompatibilityResponse,
    OptionLegResponse,
    OptionQuoteResponse,
    OptionsMarketStateResponse,
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
from services.historical_moves import get_historical_move_pcts, get_historical_move_stats
from services.llm.errors import LLMError
from services.market_expectations import get_latest_earnings_estimate, set_manual_earnings_date
from services.options_analytics import (
    compute_options_market_state,
    get_latest_close_price,
    get_latest_options_chain,
    get_latest_volatility_snapshot,
)
from services.research_history import (
    ThesisProvenance,
    delete_research_query,
    delete_thesis_version,
    get_research_query,
    get_thesis_version,
    list_research_queries,
    list_thesis_versions,
    persist_research_query,
    persist_thesis_version,
)
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


class _StrategyLabStateBar(TypedDict):
    """Shape of the market/options state fields shared by every
    StrategyLabResponse this endpoint returns -- typed so a future field
    added to one branch and forgotten in another is a real mypy error, not
    a silent inconsistency between response shapes."""

    market_session: str
    data_state: str
    snapshot_source: str | None
    snapshot_timestamp: datetime | None
    snapshot_age_minutes: int | None
    snapshot_age_label: str | None
    earnings_anchor_status: str
    options_market: OptionsMarketStateResponse


@router.post("/query", response_model=ResearchQueryResponse)
def research_query(
    body: ResearchQueryRequest,
    request: Request,
    db: DbSession,
    llm: LLM,
    orchestrator: Orchestrator,
) -> ResearchQueryResponse:
    if not request.app.state.research_rate_limiter.allow():
        raise RateLimitedError(
            "Too many research queries in a short window — each one runs several real LLM "
            "calls. Please wait a moment and try again."
        )

    result = orchestrator.run(body.question)
    trace = result.trace

    ticker = normalize_ticker(body.ticker) if body.ticker else None
    company = (
        db.query(Company).filter(Company.ticker == ticker).one_or_none()
        if ticker is not None
        else None
    )
    # Persisted only now that generation has genuinely succeeded -- a
    # failed orchestrator.run() above would have raised before reaching
    # this line, so no row is ever written for a broken/incomplete answer.
    persist_research_query(db, ticker=ticker, company=company, provider=llm.name, result=result)

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


@router.get("/history", response_model=list[AIResearchHistoryItemResponse])
def get_research_history(
    db: DbSession,
    ticker: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[AIResearchHistoryItemResponse]:
    normalized = normalize_ticker(ticker) if ticker else None
    rows = list_research_queries(db, ticker=normalized, limit=limit, offset=offset)
    return [AIResearchHistoryItemResponse.model_validate(r) for r in rows]


@router.get("/history/{query_id}", response_model=AIResearchHistoryItemResponse)
def get_research_history_item(query_id: int, db: DbSession) -> AIResearchHistoryItemResponse:
    row = get_research_query(db, query_id)
    if row is None:
        raise NotFoundError(f"no research history item with id {query_id}")
    return AIResearchHistoryItemResponse.model_validate(row)


@router.delete("/history/{query_id}", status_code=204)
def delete_research_history_item(query_id: int, db: DbSession) -> None:
    if not delete_research_query(db, query_id):
        raise NotFoundError(f"no research history item with id {query_id}")


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
        providers = build_research_providers(get_settings(), embedder, db)
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
            options_market=OptionsMarketStateResponse.model_validate(
                compute_options_market_state([], datetime.now(UTC), None)
            ),
        )

    latest_estimate = get_latest_earnings_estimate(db, company.id)
    latest_volatility = get_latest_volatility_snapshot(db, company.id)
    move_stats = get_historical_move_stats(db, company.id)
    raw_chain = get_latest_options_chain(db, company)
    options_state = compute_options_market_state(raw_chain, datetime.now(UTC), latest_volatility)

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
        latest_price=get_latest_close_price(db, ticker),
        historical_moves=(
            HistoricalMoveStatsResponse.model_validate(move_stats) if move_stats else None
        ),
        options_market=OptionsMarketStateResponse.model_validate(options_state),
    )


@router.post("/{symbol}/earnings-date", response_model=EarningsEstimateResponse)
def set_earnings_date_override(
    symbol: str, request: ManualEarningsDateRequest, db: DbSession
) -> EarningsEstimateResponse:
    """Owner/admin-only manual override for a company's next earnings
    report date -- for when no provider (Alpha Vantage) has published one
    yet, so options-chain collection and Strategy Lab aren't left
    permanently blocked on a date the provider simply hasn't listed (see
    services/research_orchestration.py::_prepare_options_chain). Persists a
    new EarningsEstimateSnapshot with date_source=manual and every
    consensus field null; never overwrites or relabels an existing row as
    provider-confirmed. See services/market_expectations.py.
    """
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    today = datetime.now(UTC).date()
    if request.estimated_report_date < today:
        raise InvalidRequestError(
            f"estimated_report_date {request.estimated_report_date} is in the past "
            f"(today is {today}) -- this is an upcoming earnings date, not a historical one"
        )

    row = set_manual_earnings_date(
        db, company, request.estimated_report_date, request.fiscal_period_end_date
    )
    return EarningsEstimateResponse.model_validate(row)


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
    """Real, ranked strategy candidates built from the most recently
    ingested real options-chain snapshot -- see
    services/strategy_generation.py and
    analytics/options/strategy_ranking.py.

    Never blocked on a known upcoming earnings date: when one is on
    record, the snapshot is earnings-anchored; when it isn't, collection
    still runs (see services/research_orchestration.py) and produces a
    general/current snapshot, which this endpoint still turns into real
    strategies -- just labeled ``anchor="general_current"`` with a
    ``reason`` disclaiming that it isn't tied to a specific earnings date,
    rather than hiding real data behind a false "nothing here" state.
    Honestly empty only when no options-chain snapshot has been collected
    at all yet.
    """
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    now = datetime.now(UTC)
    raw_chain = get_latest_options_chain(db, company)
    chain_response = [_option_quote_response(q) for q in raw_chain]
    volatility = get_latest_volatility_snapshot(db, company.id)

    options_state = compute_options_market_state(raw_chain, now, volatility)
    market_session = get_market_session(now)

    estimate_for_anchor = get_latest_earnings_estimate(db, company.id)
    if estimate_for_anchor is None or estimate_for_anchor.estimated_report_date is None:
        earnings_anchor_status = "unknown"
    else:
        earnings_anchor_status = {
            "alpha_vantage": "confirmed",
            "manual": "manual",
            "estimated": "estimated",
        }.get(estimate_for_anchor.date_source.value, "unknown")

    state_bar: _StrategyLabStateBar = {
        "market_session": market_session.session.value,
        "data_state": options_state.data_state.value,
        "snapshot_source": options_state.source,
        "snapshot_timestamp": options_state.snapshot_timestamp,
        "snapshot_age_minutes": options_state.snapshot_age_minutes,
        "snapshot_age_label": options_state.snapshot_age_label,
        "earnings_anchor_status": earnings_anchor_status,
        "options_market": OptionsMarketStateResponse.model_validate(options_state),
    }

    if volatility is None:
        if chain_response:
            # A real chain was collected (visible in `chain` below), but no
            # VolatilitySnapshot exists -- compute_and_persist_volatility_snapshot
            # returned None, almost always because no contract in the chain
            # has any bid/ask/last to price a straddle from at all (real,
            # observed live: AMD pre-market, 2026-08-18 -- every contract
            # was FROZEN with real IV/Greeks but null bid/ask/last).
            reason = (
                f"A real options-chain snapshot exists for {ticker} ({len(chain_response)} "
                "contracts, shown below), but no live or last-known price (bid/ask/last) "
                "exists on any contract yet to compute an implied move or generate strategy "
                "candidates from -- common before market open, when quotes are frozen with a "
                "quality/Greeks read but no tradable price."
            )
        else:
            estimate = get_latest_earnings_estimate(db, company.id)
            if estimate is None or estimate.estimated_report_date is None:
                reason = (
                    f"{ticker}'s next earnings date isn't known yet -- the analyst-estimates "
                    "provider has no upcoming report date on record for this company right now. "
                    "No options-chain snapshot has been collected yet either."
                )
            else:
                reason = (
                    f"{ticker}'s next earnings date is expected around "
                    f"{estimate.estimated_report_date}, but no real options-chain snapshot has "
                    "been collected for it yet."
                )
        return StrategyLabResponse(
            ticker=ticker,
            expiration=None,
            underlying_price=None,
            implied_move_pct=None,
            strategies=[],
            chain=chain_response,
            reason=reason,
            **state_bar,
        )

    is_general = volatility.target_earnings_date is None
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

    if not candidates:
        return StrategyLabResponse(
            ticker=ticker,
            expiration=None,
            underlying_price=None,
            implied_move_pct=volatility.implied_move_pct,
            strategies=[],
            chain=chain_response,
            anchor=volatility.anchor.value,
            reason=(
                f"A real options-chain snapshot exists for {ticker}, but no strategy "
                "candidates could be generated from it -- possibly no underlying price is "
                "on record as of the snapshot, or no expiration in the chain matches."
            ),
            **state_bar,
        )

    return StrategyLabResponse(
        ticker=ticker,
        expiration=candidates[0].expiration,
        underlying_price=candidates[0].underlying_price,
        implied_move_pct=volatility.implied_move_pct,
        strategies=strategies,
        chain=chain_response,
        anchor=volatility.anchor.value,
        reason=(
            "Next earnings date is not currently confirmed, so this option snapshot is not "
            "earnings-anchored."
        )
        if is_general
        else None,
        **state_bar,
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

    # Persisted as a new version only now that generation has genuinely
    # succeeded -- never overwrites a prior version (see
    # models/ai_thesis_version.py).
    persist_thesis_version(
        db,
        company=company,
        provider=llm.name,
        result=result,
        provenance=ThesisProvenance(
            estimate_snapshot_id=result.estimate_snapshot_id,
            volatility_snapshot_id=result.volatility_snapshot_id,
        ),
    )

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


def _thesis_is_stale(db: DbSession, company: Company, version: AIThesisVersion) -> bool:
    """True when the real *current* latest consensus/options snapshot for
    ``company`` is a different row than what this version was grounded in
    -- never inferred from a fixed time window, only from an actual change
    in which snapshot is now the latest."""
    current_estimate = get_latest_earnings_estimate(db, company.id)
    current_volatility = get_latest_volatility_snapshot(db, company.id)
    current_estimate_id = current_estimate.id if current_estimate is not None else None
    current_volatility_id = current_volatility.id if current_volatility is not None else None
    return (
        version.earnings_estimate_snapshot_id != current_estimate_id
        or version.volatility_snapshot_id != current_volatility_id
    )


@router.get("/{symbol}/theses", response_model=list[AIThesisVersionResponse])
def get_thesis_history(
    symbol: str,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[AIThesisVersionResponse]:
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    versions = list_thesis_versions(db, company.id, limit=limit, offset=offset)
    responses = []
    for v in versions:
        response = AIThesisVersionResponse.model_validate(v)
        response.is_stale = _thesis_is_stale(db, company, v)
        responses.append(response)
    return responses


@router.get("/{symbol}/theses/{version_id}", response_model=AIThesisVersionResponse)
def get_thesis_version_item(
    symbol: str, version_id: int, db: DbSession
) -> AIThesisVersionResponse:
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    version = get_thesis_version(db, version_id)
    if version is None or version.company_id != company.id:
        raise NotFoundError(f"no thesis version {version_id} for {ticker!r}")
    response = AIThesisVersionResponse.model_validate(version)
    response.is_stale = _thesis_is_stale(db, company, version)
    return response


@router.delete("/{symbol}/theses/{version_id}", status_code=204)
def delete_thesis_version_item(symbol: str, version_id: int, db: DbSession) -> None:
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    version = get_thesis_version(db, version_id)
    if version is None or version.company_id != company.id:
        raise NotFoundError(f"no thesis version {version_id} for {ticker!r}")
    delete_thesis_version(db, version_id)
