import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TypedDict

from fastapi import APIRouter, BackgroundTasks, Query, Request

from analytics.decision.budget import validate_risk_cap_inputs
from analytics.decision.probability import build_estimated_probability
from analytics.market_session import get_market_session
from analytics.options.expiration_methodology_comparison import (
    compare_expiration_methodologies,
)
from analytics.options.expiration_selection import ExpirationCandidate, ExpirationSelectionResult
from analytics.options.move_compatibility import (
    MoveCompatibility,
    assess_move_compatibility,
    assess_move_compatibility_from_values,
)
from analytics.options.strategy_ranking import rank_strategy_candidates
from api.deps import LLM, DbSession, Embedder, Orchestrator
from api.exceptions import InvalidRequestError, NotFoundError, RateLimitedError
from core.config import get_settings
from db.session import SessionLocal
from models.ai_decision_version import AIDecisionVersion
from models.company import Company
from models.document_chunk import DocumentChunk
from models.earnings_event import EarningsEvent
from models.enums import (
    DecisionDirection,
    DecisionSource,
    DecisionStatus,
    DecisionVolatilityView,
    RiskProfile,
)
from models.filing import Filing
from models.price_bar import PriceBar
from models.research_preparation_job import JobStatus
from providers.factory import (
    MissingOptionsProviderConfigError,
    UnknownOptionsProviderError,
    get_options_provider,
)
from providers.sec_edgar import SECEdgarProvider
from providers.types import OptionQuote
from rag.context import assemble_context
from rag.embeddings import EmbeddingProvider
from rag.retrieval import RetrievalFilters, hybrid_search
from schemas.api import (
    AIDecisionVersionResponse,
    AIResearchHistoryItemResponse,
    AIThesisVersionResponse,
    CitationResponse,
    CompanyResponse,
    ConfidenceBucketResponse,
    DecisionGenerateRequest,
    EarningsEstimateResponse,
    EarningsThesisResponse,
    EstimatedProbabilityResponse,
    ExecutionTraceResponse,
    ExpirationCandidateResponse,
    ExpirationMethodologyComparisonResponse,
    ExpirationScoreResponse,
    ExpirationSelectionResponse,
    FilingSearchResponse,
    HistoricalMoveStatsResponse,
    ManualEarningsDateRequest,
    MoveCompatibilityResponse,
    OptionLegResponse,
    OptionQuoteResponse,
    OptionsMarketStateResponse,
    PendingDecisionResponse,
    PendingDecisionsResponse,
    PreparingCompanyResponse,
    RankedStrategyResponse,
    RateResponse,
    ResearchJobQueuedResponse,
    ResearchJobResponse,
    ResearchOverviewResponse,
    ResearchQueryRequest,
    ResearchQueryResponse,
    ResearchQueryStatus,
    ScenarioPnlResponse,
    SettlementAttemptResponse,
    StrategyAnalysisResponse,
    StrategyLabResponse,
    ToolCallResponse,
    TrackRecordResponse,
    VolatilitySnapshotResponse,
)
from services.decision_engine import (
    DecisionGenerationError,
    generate_decision,
)
from services.decision_history import (
    MAX_HISTORY_LIMIT,
    DecisionListFilters,
    delete_decision,
    get_decision,
    list_all_decisions,
    list_decisions,
    mark_final,
    persist_decision,
)
from services.decision_settlement import compute_settlement_eligibility, settle_decision
from services.earnings_research_preparation import enqueue_ticker_for_preparation
from services.earnings_thesis import ThesisGenerationError, generate_earnings_thesis
from services.expiration_engine import (
    DEFAULT_MAX_CANDIDATES,
    resolve_auto_expiration,
    resolve_manual_expiration,
)
from services.historical_moves import get_historical_move_pcts, get_historical_move_stats
from services.llm.errors import LLMError
from services.market_expectations import get_latest_earnings_estimate, set_manual_earnings_date
from services.options_analytics import (
    PricingSnapshotSelection,
    compute_and_persist_volatility_snapshot,
    compute_options_market_state,
    get_latest_close_price,
    get_latest_volatility_snapshot,
)
from services.options_reconstruction import resolve_best_actionable_option_market
from services.provider_settings import get_app_provider_settings
from services.research_history import (
    ThesisProvenance,
    delete_research_query,
    delete_thesis_version,
    get_research_query,
    get_thesis_version,
    is_thesis_stale,
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
from services.research_query_resolution import resolve_mentioned_companies
from services.strategy_generation import generate_strategy_candidates
from services.symbol_resolution import normalize_ticker, resolve_symbol
from services.track_record import compute_track_record

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

    settings = get_settings()
    edgar = SECEdgarProvider(user_agent=settings.sec_edgar_user_agent)
    resolution = resolve_mentioned_companies(db, edgar, body.question, explicit_ticker=body.ticker)

    if body.ticker:
        explicit = normalize_ticker(body.ticker)
        if not any(r.ticker == explicit for r in resolution.resolved):
            # The one company this request explicitly named (e.g. the
            # Research page's own ?ticker= context) isn't a real,
            # SEC-known company at all -- an honest, definitive state
            # (Part A11), not a guess dressed up as an answer.
            return ResearchQueryResponse(
                question=body.question,
                status="company_not_found",
                unresolved_tickers=[explicit],
            )

    # Part A4 -- reuse a company's research immediately if it's already
    # ready; for anything that isn't, enqueue through the SAME durable
    # queue the automated scheduler uses (never run preparation inline
    # in this request) and report it honestly rather than answering from
    # nothing.
    ready_tickers: list[str] = []
    preparing: list[PreparingCompanyResponse] = []
    for r in resolution.resolved:
        job = get_latest_research_job(db, r.ticker)
        if job is not None and job.status in (
            JobStatus.COMPLETED,
            JobStatus.COMPLETED_WITH_WARNINGS,
        ):
            ready_tickers.append(r.ticker)
        else:
            queued_job = enqueue_ticker_for_preparation(db, r.ticker)
            preparing.append(
                PreparingCompanyResponse(
                    ticker=r.ticker,
                    job_id=queued_job.id,
                    job_status=queued_job.status.value,
                )
            )

    if resolution.resolved and not ready_tickers:
        # Every real, resolved company this question is about still
        # needs preparation -- nothing to honestly answer with yet.
        return ResearchQueryResponse(
            question=body.question,
            status="preparing",
            preparing=preparing,
            unresolved_tickers=resolution.unresolved,
        )

    result = orchestrator.run(
        body.question, resolved_tickers=ready_tickers or None, as_of=body.as_of
    )
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

    # Part A11 -- honest, real-signal detection, not a guess: only when
    # this question was scoped to a specific real company (ready_tickers
    # non-empty), every tool call it tried genuinely failed, and no
    # citation was produced either.
    all_tool_calls_failed = bool(trace.tool_calls) and all(
        not tc.success for tc in trace.tool_calls
    )
    status: ResearchQueryStatus = (
        "insufficient_evidence"
        if ready_tickers and all_tool_calls_failed and not result.citations
        else "completed"
    )

    return ResearchQueryResponse(
        question=result.question,
        status=status,
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
        preparing=preparing,
        unresolved_tickers=resolution.unresolved,
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
    now = datetime.now(UTC)
    earnings_date_for_resolution = (
        latest_estimate.estimated_report_date if latest_estimate is not None else None
    )
    resolution = resolve_best_actionable_option_market(
        db, company, now, earnings_date_for_resolution
    )
    selection = resolution.selection
    volatility_is_stale = (
        latest_volatility is None
        or latest_volatility.snapshot_timestamp != selection.snapshot_timestamp
    )
    if selection.quotes and volatility_is_stale:
        # The selected snapshot (e.g. freshly reconstructed, or a
        # persisted close/near-close snapshot that never had its implied
        # move computed) has no matching VolatilitySnapshot on record yet
        # -- compute and persist one now rather than showing stale/absent
        # implied-move data next to genuinely usable pricing.
        computed = compute_and_persist_volatility_snapshot(
            db, company, earnings_date_for_resolution, selection=selection
        )
        if computed is not None:
            latest_volatility = computed
    options_state = compute_options_market_state(
        selection.quotes, now, latest_volatility, selection
    )

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


def _get_options_provider_for_request(db: DbSession):
    """Wraps providers.factory.get_options_provider for the real-time,
    request-triggered call sites (expiration comparison, manual expiration
    in Strategy Lab/AI Decision) -- turns an unconfigured/unknown options
    provider into a clean, actionable 422 rather than an opaque 500, since
    this is a client-fixable configuration state (see Settings > Data
    Providers), not a server bug."""
    settings = get_settings()
    overrides = get_app_provider_settings(db)
    try:
        return get_options_provider(settings, overrides.options_primary, db)
    except (MissingOptionsProviderConfigError, UnknownOptionsProviderError) as exc:
        raise InvalidRequestError(
            f"no options data provider is available for this request: {exc}. "
            "Configure one in Settings > Data Providers."
        ) from exc


def _expiration_candidate_response(candidate: ExpirationCandidate) -> ExpirationCandidateResponse:
    return ExpirationCandidateResponse(
        expiration=candidate.expiration,
        dte=candidate.dte,
        days_after_earnings=candidate.days_after_earnings,
        contract_count=candidate.contract_count,
        priceable_contract_count=candidate.priceable_contract_count,
        quote_coverage=candidate.quote_coverage,
        bid_ask_coverage=candidate.bid_ask_coverage,
        oi_coverage=candidate.oi_coverage,
        volume_coverage=candidate.volume_coverage,
        atm_iv=candidate.atm_iv,
        atm_spread_pct=candidate.atm_spread_pct,
        quality=candidate.quality,
        score=ExpirationScoreResponse(**candidate.score.as_dict()),
        is_earnings_anchored=candidate.is_earnings_anchored,
        excluded_pre_earnings=candidate.excluded_pre_earnings,
    )


def _expiration_selection_response(
    result: ExpirationSelectionResult,
    earnings_date: date | None = None,
) -> ExpirationSelectionResponse:
    # Phase 4 methodology-experiments hardening (2026-08-26), Section 35 --
    # EXPERIMENTAL only, mode="auto" with a real earnings date to compare
    # against; zero extra provider calls (reuses this same result's own
    # already-discovered candidates). Never touches the official
    # BenchmarkPortfolio methodology.
    comparison = None
    if result.mode == "auto" and earnings_date is not None:
        comparison = ExpirationMethodologyComparisonResponse.model_validate(
            compare_expiration_methodologies(result, earnings_date)
        )
    return ExpirationSelectionResponse(
        mode=result.mode,
        selected=(
            _expiration_candidate_response(result.selected) if result.selected is not None else None
        ),
        alternatives=[_expiration_candidate_response(c) for c in result.alternatives],
        reasons=result.reasons,
        warning=result.warning,
        methodology_comparison=comparison,
    )


@router.get("/{symbol}/expirations", response_model=ExpirationSelectionResponse)
def get_expiration_selection(
    symbol: str,
    db: DbSession,
    mode: str = Query(default="auto", pattern="^(auto|manual)$"),
    expiration: date | None = Query(  # noqa: B008 -- FastAPI's own dependency-injection idiom
        default=None, description="Required when mode=manual"
    ),
    max_candidates: int = Query(default=DEFAULT_MAX_CANDIDATES, ge=1, le=6),
) -> ExpirationSelectionResponse:
    """Options Decision Engine V3 Part C -- real, live comparison of
    multiple candidate expirations (see services/expiration_engine.py),
    distinct from the single-expiration pick the market-data resolver
    (services/options_reconstruction.py) already makes for Strategy Lab's
    default state bar. ``mode=auto`` discovers and scores real listed
    expirations after the earnings date (or after now, in general mode).
    ``mode=manual`` requires ``expiration`` and evaluates only that real,
    user-chosen date -- flagged (never blocked) if it scores materially
    worse than Auto's own pick.
    """
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")
    if mode == "manual" and expiration is None:
        raise InvalidRequestError("mode=manual requires an `expiration` query parameter")

    provider = _get_options_provider_for_request(db)

    now = datetime.now(UTC)
    estimate = get_latest_earnings_estimate(db, company.id)
    earnings_date = estimate.estimated_report_date if estimate is not None else None

    if mode == "manual":
        assert expiration is not None  # guarded above
        result = resolve_manual_expiration(
            db, company, provider, now, earnings_date, expiration, max_candidates=max_candidates
        )
    else:
        result = resolve_auto_expiration(
            db, company, provider, now, earnings_date, max_candidates=max_candidates
        )
    return _expiration_selection_response(result, earnings_date)


@router.get("/{symbol}/strategies", response_model=StrategyLabResponse)
def get_strategy_lab(
    symbol: str,
    db: DbSession,
    expiration: date | None = Query(  # noqa: B008 -- FastAPI's own dependency-injection idiom
        default=None,
        description="Manual expiration override (real, from GET .../expirations). "
        "When set, all contracts/strategies are recomputed from exactly this expiration.",
    ),
) -> StrategyLabResponse:
    """Real, ranked strategy candidates built from the most recently
    ingested real options-chain snapshot -- see
    services/strategy_generation.py and
    analytics/options/strategy_ranking.py.

    Market-focused only (Phase 14.12): real chain, expiration, strikes,
    premiums, liquidity, IV/Greeks, payoff, and historical-move
    compatibility. Deliberately carries no budget/risk-cap parameters --
    "what strategies exist in the real market" is a different question
    from "what should I trade given my budget," and mixing them here
    previously produced a "Not feasible for $X budget" message on a chain
    that had no priceable candidates at all, conflating two genuinely
    different reasons a candidate might be absent. Budget-aware sizing
    lives exclusively in AI Decision (services/decision_engine.py).

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
    estimate_for_anchor = get_latest_earnings_estimate(db, company.id)
    earnings_date_for_resolution = (
        estimate_for_anchor.estimated_report_date if estimate_for_anchor is not None else None
    )
    if expiration is not None:
        provider = _get_options_provider_for_request(db)
        quotes = provider.get_option_chain(ticker, now, expiration=expiration)
        priceable = [q for q in quotes if q.bid is not None or q.ask is not None or q.last_price]
        selection = PricingSnapshotSelection(
            quotes=quotes,
            tier="current_priceable" if priceable else ("contracts_only" if quotes else "none"),
            is_fallback=False,
            purpose="intraday",
            snapshot_timestamp=now,
        )
    else:
        resolution = resolve_best_actionable_option_market(
            db, company, now, earnings_date_for_resolution
        )
        selection = resolution.selection
    raw_chain = selection.quotes
    chain_response = [_option_quote_response(q) for q in raw_chain]
    volatility = get_latest_volatility_snapshot(db, company.id)
    if raw_chain and (
        volatility is None or volatility.snapshot_timestamp != selection.snapshot_timestamp
    ):
        computed = compute_and_persist_volatility_snapshot(
            db, company, earnings_date_for_resolution, selection=selection
        )
        if computed is not None:
            volatility = computed

    options_state = compute_options_market_state(raw_chain, now, volatility, selection)
    market_session = get_market_session(now)

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

    if options_state.actionability == "stale_research_only":
        # Phase 14.11 Part 4: a HARD GATE, not merely a label -- a
        # snapshot two or more real US trading sessions old must never
        # back a generated strategy recommendation, no matter how
        # complete its pricing looked at capture time.
        return StrategyLabResponse(
            ticker=ticker,
            expiration=None,
            underlying_price=None,
            implied_move_pct=None,
            strategies=[],
            chain=chain_response,
            reason=options_state.reason,
            **state_bar,
        )

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
    candidates = generate_strategy_candidates(
        db, company, volatility.target_earnings_date, selection
    )
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
        response.is_stale = is_thesis_stale(db, company, v)
        responses.append(response)
    return responses


@router.get("/{symbol}/theses/{version_id}", response_model=AIThesisVersionResponse)
def get_thesis_version_item(symbol: str, version_id: int, db: DbSession) -> AIThesisVersionResponse:
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    version = get_thesis_version(db, version_id)
    if version is None or version.company_id != company.id:
        raise NotFoundError(f"no thesis version {version_id} for {ticker!r}")
    response = AIThesisVersionResponse.model_validate(version)
    response.is_stale = is_thesis_stale(db, company, version)
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


def _historical_compatibility_for_decision(
    db: DbSession, decision: AIDecisionVersion
) -> MoveCompatibility | None:
    """Options Decision Engine V3 Part E: reconstructed at READ time from
    the persisted recommended_strategy_analysis + underlying_price against
    the CURRENT real historical-move sample -- never persisted itself, so
    it can only get more accurate (a bigger real sample) as more real
    earnings events are reported, never stale relative to that."""
    if decision.recommended_strategy_analysis is None or decision.underlying_price is None:
        return None
    analysis = decision.recommended_strategy_analysis
    breakevens = [Decimal(b) for b in analysis.get("breakevens", [])]
    net_premium_str = analysis.get("net_premium")
    if not breakevens or net_premium_str is None:
        return None
    historical_moves = get_historical_move_pcts(db, decision.company_id)
    return assess_move_compatibility_from_values(
        breakevens=breakevens,
        net_premium=Decimal(net_premium_str),
        underlying_price=decision.underlying_price,
        historical_move_pcts=historical_moves,
    )


def _decision_response(db: DbSession, decision: AIDecisionVersion) -> AIDecisionVersionResponse:
    """Every decision response embeds its real, freshly computed
    settlement eligibility (Phase 14.10 Part I) -- computed live, never
    persisted, so the frontend can render the right "Attempt Settlement"
    button state (or none at all) without a second round trip, and so it
    can never go stale relative to real data that just arrived. Options
    Decision Engine V3 Part E: historical_compatibility/estimated_probability
    are computed the same way, for the same reason."""
    eligibility = compute_settlement_eligibility(db, decision)
    compatibility = _historical_compatibility_for_decision(db, decision)
    estimated_probability = build_estimated_probability(compatibility)
    data = AIDecisionVersionResponse.model_validate(decision).model_dump()
    data["settlement_eligible"] = eligibility.eligible
    data["settlement_state"] = eligibility.state
    data["settlement_reason"] = eligibility.reason
    data["settlement_earliest_date"] = eligibility.earliest_settlement_date
    data["historical_compatibility"] = (
        MoveCompatibilityResponse(
            method=compatibility.method,
            sample_size=compatibility.sample_size,
            requires_move_beyond_threshold=compatibility.requires_move_beyond_threshold,
            required_move_pct=compatibility.required_move_pct,
            compatible_count=compatibility.compatible_count,
            compatible_pct=compatibility.compatible_pct,
        )
        if compatibility is not None
        else None
    )
    data["estimated_probability"] = (
        EstimatedProbabilityResponse(
            method=estimated_probability.method,
            sample_size=estimated_probability.sample_size,
            compatible_count=estimated_probability.compatible_count,
            probability=estimated_probability.probability,
            low_sample_confidence=estimated_probability.low_sample_confidence,
            wilson_lower=estimated_probability.wilson_lower,
            wilson_upper=estimated_probability.wilson_upper,
        )
        if estimated_probability is not None
        else None
    )
    return AIDecisionVersionResponse(**data)


@router.post("/{symbol}/decision", response_model=AIDecisionVersionResponse)
def generate_decision_endpoint(
    symbol: str,
    request: Request,
    db: DbSession,
    llm: LLM,
    embedder: Embedder,
    body: DecisionGenerateRequest | None = None,
) -> AIDecisionVersionResponse:
    if not request.app.state.research_rate_limiter.allow():
        raise RateLimitedError(
            "Too many AI requests in a short window — a decision runs several real LLM calls. "
            "Please wait a moment and try again."
        )
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r} — prepare it first")

    direction_override = None
    volatility_view_override = None
    decision_source = DecisionSource.AI
    if body is not None and body.direction is not None and body.volatility_view is not None:
        try:
            direction_override = DecisionDirection(body.direction)
            volatility_view_override = DecisionVolatilityView(body.volatility_view)
        except ValueError as exc:
            raise InvalidRequestError(f"unrecognized direction or volatility_view: {exc}") from exc
        decision_source = DecisionSource.MANUAL_OVERRIDE

    trade_budget = body.trade_budget if body is not None else None
    risk_cap = body.risk_cap if body is not None else None
    risk_cap_is_percent = body.risk_cap_is_percent if body is not None else False

    risk_profile: RiskProfile | None = None
    if body is not None and body.risk_profile is not None:
        try:
            risk_profile = RiskProfile(body.risk_profile)
        except ValueError as exc:
            raise InvalidRequestError(f"unrecognized risk_profile: {exc}") from exc

    try:
        validate_risk_cap_inputs(risk_cap, risk_cap_is_percent)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc

    try:
        result = generate_decision(
            db,
            llm,
            embedder,
            company,
            direction_override=direction_override,
            volatility_view_override=volatility_view_override,
            trade_budget=trade_budget,
            risk_cap=risk_cap,
            risk_cap_is_percent=risk_cap_is_percent,
            risk_profile=risk_profile,
            manual_expiration=body.expiration if body is not None else None,
        )
    except DecisionGenerationError as exc:
        raise LLMError(f"decision generation failed: {exc}") from exc
    except (MissingOptionsProviderConfigError, UnknownOptionsProviderError) as exc:
        raise InvalidRequestError(
            f"manual expiration requires a configured options data provider: {exc}. "
            "Configure one in Settings > Data Providers."
        ) from exc

    row = persist_decision(db, company=company, result=result, decision_source=decision_source)
    return _decision_response(db, row)


@router.get("/{symbol}/decisions", response_model=list[AIDecisionVersionResponse])
def get_decision_history(
    symbol: str,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[AIDecisionVersionResponse]:
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    decisions = list_decisions(db, company.id, limit=limit, offset=offset)
    return [_decision_response(db, d) for d in decisions]


@router.get("/{symbol}/decisions/{decision_id}", response_model=AIDecisionVersionResponse)
def get_decision_item(symbol: str, decision_id: int, db: DbSession) -> AIDecisionVersionResponse:
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    decision = get_decision(db, decision_id)
    if decision is None or decision.company_id != company.id:
        raise NotFoundError(f"no decision {decision_id} for {ticker!r}")
    return _decision_response(db, decision)


@router.delete("/{symbol}/decisions/{decision_id}", status_code=204)
def delete_decision_item(symbol: str, decision_id: int, db: DbSession) -> None:
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    decision = get_decision(db, decision_id)
    if decision is None or decision.company_id != company.id:
        raise NotFoundError(f"no decision {decision_id} for {ticker!r}")
    delete_decision(db, decision_id)


@router.post("/{symbol}/decisions/{decision_id}/final", response_model=AIDecisionVersionResponse)
def mark_decision_final(symbol: str, decision_id: int, db: DbSession) -> AIDecisionVersionResponse:
    """Marks ``decision_id`` as the Final Decision for this company (Phase
    14.9 Part F section 22) -- the one used for post-event track-record
    evaluation. Unmarks any other decision that was previously final for
    the same company; does not touch decisions for other companies."""
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    decision = get_decision(db, decision_id)
    if decision is None or decision.company_id != company.id:
        raise NotFoundError(f"no decision {decision_id} for {ticker!r}")
    updated = mark_final(db, decision_id)
    assert updated is not None
    return _decision_response(db, updated)


@router.post("/{symbol}/decisions/{decision_id}/settle", response_model=SettlementAttemptResponse)
def settle_decision_item(symbol: str, decision_id: int, db: DbSession) -> SettlementAttemptResponse:
    """Attempts settlement now rather than waiting for the next scheduled
    pass (see services/decision_settlement.py). Every outcome -- already
    settled, not yet eligible (with the real reason why), a genuine
    settlement failure despite eligibility, or a genuine success -- is a
    distinct, real response; never a silent no-op (Phase 14.10 Part I3)."""
    ticker = normalize_ticker(symbol)
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        raise NotFoundError(f"no research on record yet for {ticker!r}")

    decision = get_decision(db, decision_id)
    if decision is None or decision.company_id != company.id:
        raise NotFoundError(f"no decision {decision_id} for {ticker!r}")

    eligibility = compute_settlement_eligibility(db, decision)
    if eligibility.state == "settled":
        return SettlementAttemptResponse(
            decision=_decision_response(db, decision),
            settled=True,
            message="This decision was already settled.",
        )
    if not eligibility.eligible:
        return SettlementAttemptResponse(
            decision=_decision_response(db, decision),
            settled=False,
            message=eligibility.reason,
        )

    updated = settle_decision(db, decision)
    if updated is None:
        return SettlementAttemptResponse(
            decision=_decision_response(db, decision),
            settled=False,
            message="Settlement failed: real post-earnings data was not available after all.",
        )
    return SettlementAttemptResponse(
        decision=_decision_response(db, updated),
        settled=True,
        message="Decision settled successfully.",
    )


@router.get("/decisions/pending", response_model=PendingDecisionsResponse)
def list_pending_final_decisions(db: DbSession) -> PendingDecisionsResponse:
    """Every Final Decision across every company, split by real settlement
    status (Phase 14.10 Part I4) -- lets Track Record surface what's still
    waiting on a real post-earnings outcome, and settle it directly from
    there, without navigating into each company's Decision tab one by
    one. Bounded by MAX_HISTORY_LIMIT for the same reason as every other
    cross-company list in this project (see services/decision_history.py):
    a personal research tool with a small real decision count, never an
    unbounded batch query."""
    pending_rows = list_all_decisions(
        db,
        filters=DecisionListFilters(is_final_only=True, status=DecisionStatus.OPEN),
        limit=MAX_HISTORY_LIMIT,
    )
    final_count = db.query(AIDecisionVersion).filter(AIDecisionVersion.is_final.is_(True)).count()
    settled_count = (
        db.query(AIDecisionVersion)
        .filter(
            AIDecisionVersion.is_final.is_(True),
            AIDecisionVersion.status == DecisionStatus.SETTLED,
        )
        .count()
    )
    return PendingDecisionsResponse(
        pending=[
            PendingDecisionResponse(ticker=d.company.ticker, decision=_decision_response(db, d))
            for d in pending_rows
        ],
        final_count=final_count,
        pending_count=len(pending_rows),
        settled_count=settled_count,
    )


@router.get("/track-record", response_model=TrackRecordResponse)
def get_track_record(
    db: DbSession,
    ticker: str | None = None,
    window: str = Query(default="all_time", pattern="^(all_time|last_10)$"),
) -> TrackRecordResponse:
    normalized = normalize_ticker(ticker) if ticker else None
    summary = compute_track_record(db, ticker=normalized, window=window)  # type: ignore[arg-type]
    return TrackRecordResponse(
        window=summary.window,
        evaluated_count=summary.evaluated_count,
        directional_accuracy=RateResponse(
            correct=summary.directional_accuracy.correct,
            total=summary.directional_accuracy.total,
            pct=summary.directional_accuracy.pct,
        ),
        bullish_accuracy=RateResponse(
            correct=summary.bullish_accuracy.correct,
            total=summary.bullish_accuracy.total,
            pct=summary.bullish_accuracy.pct,
        ),
        bearish_accuracy=RateResponse(
            correct=summary.bearish_accuracy.correct,
            total=summary.bearish_accuracy.total,
            pct=summary.bearish_accuracy.pct,
        ),
        average_confidence=summary.average_confidence,
        high_confidence_accuracy=RateResponse(
            correct=summary.high_confidence_accuracy.correct,
            total=summary.high_confidence_accuracy.total,
            pct=summary.high_confidence_accuracy.pct,
        ),
        volatility_view_accuracy=RateResponse(
            correct=summary.volatility_view_accuracy.correct,
            total=summary.volatility_view_accuracy.total,
            pct=summary.volatility_view_accuracy.pct,
        ),
        breakeven_success=RateResponse(
            correct=summary.breakeven_success.correct,
            total=summary.breakeven_success.total,
            pct=summary.breakeven_success.pct,
        ),
        strategy_win_rate_available=summary.strategy_win_rate_available,
        confidence_calibration=[
            ConfidenceBucketResponse(
                label=b.label,
                lower=b.lower,
                upper=b.upper,
                rate=RateResponse(correct=b.rate.correct, total=b.rate.total, pct=b.rate.pct),
            )
            for b in summary.confidence_calibration
        ],
    )
