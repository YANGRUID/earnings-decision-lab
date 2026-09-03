"""Executes the on-demand research-preparation pipeline for a single ticker
(Phase 14) -- the backend half of "prepare/refresh research data", the
first real step in the product's core workflow. Reuses existing
per-company ingestion helpers (ingestion.bootstrap_phase1,
ingestion.backfill_earnings_dates, ingestion.backfill_price_data,
ingestion.ingest_filing_chunks, services.market_expectations,
services.options_analytics) rather than rebuilding their logic -- this
module's own job is orchestration and freshness-gated step tracking, not
raw data fetching.

Idempotent by construction: every step first checks
``analytics.research.freshness`` against that data class's own persisted
timestamp and skips the real fetch when data is already fresh, so calling
this twice in a row for the same symbol makes at most one real network
call per data class rather than one per call.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from analytics.research.freshness import DataClass, assess_freshness, needs_refresh
from core.config import Settings
from ingestion.backfill_earnings_dates import _backfill_earnings_dates, _backfill_period_end_dates
from ingestion.backfill_price_data import (
    MARKET_TICKER,
    SECTOR_TICKER,
    _backfill_snapshots,
    _ingest_price_bars,
    _load_close_series,
)
from ingestion.bootstrap_phase1 import (
    _FP_TO_QUARTER,
    _index_by_period,
    _upsert_company,
    _upsert_earnings_event_and_result,
    _upsert_filing,
)
from ingestion.ingest_filing_chunks import ingest_filing
from models.company import Company
from models.document_chunk import DocumentChunk
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.enums import FilingType
from models.filing import Filing
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.price_reaction import PriceReaction
from models.research_preparation_job import (
    REQUIRED_STEPS,
    JobStatus,
    PreparationStep,
    ResearchPreparationJob,
    StepStatus,
)
from providers.alpha_vantage_estimates import AlphaVantageEarningsEstimatesProvider
from providers.base import EarningsEstimatesProvider, MarketDataProvider, OptionsDataProvider
from providers.factory import build_market_data_chain, build_options_provider_chain
from providers.sec_edgar import SECEdgarProvider
from rag.embeddings import EmbeddingProvider
from services.llm.base import LLMProvider
from services.llm.factory import get_llm_provider
from services.market_expectations import (
    collect_next_earnings_estimate_calendar_first,
    get_latest_earnings_estimate,
)
from services.options_analytics import (
    collect_options_snapshot_now,
    compute_and_persist_volatility_snapshot,
    get_implied_vs_realized_moves,
)
from services.provider_settings import get_app_provider_settings
from services.secret_store import resolve_secret
from services.symbol_resolution import SymbolResolution, resolve_symbol
from services.usage_instrumentation import instrument_data_provider

log = logging.getLogger("research_orchestration")


class UnsupportedSymbolError(Exception):
    """Raised when ``resolve_symbol`` reports a ticker this project cannot
    honestly research -- the caller (API layer) is expected to turn this
    into a clear 4xx response, never a fabricated result."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ResearchProviders:
    edgar: SECEdgarProvider
    market_data: MarketDataProvider
    estimates: EarningsEstimatesProvider | None
    options: OptionsDataProvider | None
    embedder: EmbeddingProvider
    #: The general-purpose LLM (DEEPSEEK_MODEL, thinking off) used for the
    #: AI thesis step. None when no LLM is configured -- the step is then
    #: SKIPPED honestly rather than failing the whole preparation.
    llm: LLMProvider | None = None


def _build_thesis_llm(settings: Settings, db: Session | None) -> LLMProvider | None:
    """The general-purpose model for the preparation thesis step -- never the
    V4 DecisionView model, which is a separate, versioned configuration."""
    try:
        return get_llm_provider(settings, db=db)
    except Exception as exc:  # noqa: BLE001 -- unconfigured LLM must not block data preparation
        log.warning("research preparation: no LLM available for the thesis step: %s", exc)
        return None


def build_research_providers(
    settings: Settings, embedder: EmbeddingProvider, db: Session | None = None
) -> ResearchProviders:
    """Constructs every provider the pipeline can use from real app config,
    plus any owner-configured provider-selection override (see
    services/provider_settings.py) -- read fresh from ``db`` on every call,
    never cached, so changing a provider in the Settings UI takes effect on
    the very next research run. ``db`` is optional only so callers that
    genuinely have no session yet (none exist today) still type-check;
    every real caller passes one.

    ``estimates`` and ``options`` are None -- not raised -- when their
    configuration is missing, since both are optional preparation steps
    (see REQUIRED_STEPS): a missing Alpha Vantage key or unset
    OPTIONS_PROVIDER should degrade those two steps to SKIPPED, not fail
    the whole pipeline. ``market_data`` has no such fallback -- price
    history is required, so a genuinely unconfigured deployment (no Tiingo
    or Alpha Vantage key at all) is left to raise, the same real
    RuntimeError ``ingestion.backfill_price_data.main`` already raises.
    """
    overrides = get_app_provider_settings(db) if db is not None else None

    edgar = instrument_data_provider(
        SECEdgarProvider(user_agent=settings.sec_edgar_user_agent), db, "sec_edgar", "filings"
    )
    market_data = build_market_data_chain(
        settings,
        primary_override=overrides.price_history_primary if overrides else None,
        fallback_override=overrides.price_history_fallback if overrides else None,
        db=db,
    )

    estimates: EarningsEstimatesProvider | None = None
    alpha_vantage_key = resolve_secret(settings, "alpha_vantage", db)
    if alpha_vantage_key:
        estimates = instrument_data_provider(
            AlphaVantageEarningsEstimatesProvider(api_key=alpha_vantage_key),
            db,
            "alpha_vantage",
            "earnings_estimates",
        )

    options = build_options_provider_chain(
        settings,
        primary_override=overrides.options_primary if overrides else None,
        fallback_override=overrides.options_fallback if overrides else None,
        db=db,
    )

    return ResearchProviders(
        edgar=edgar,
        market_data=market_data,
        estimates=estimates,
        options=options,
        embedder=embedder,
        llm=_build_thesis_llm(settings, db),
    )


def get_latest_research_job(db: Session, ticker: str) -> ResearchPreparationJob | None:
    return (
        db.query(ResearchPreparationJob)
        .filter(ResearchPreparationJob.ticker == ticker)
        .order_by(ResearchPreparationJob.started_at.desc())
        .first()
    )


def get_running_research_job(db: Session, ticker: str) -> ResearchPreparationJob | None:
    """The already-in-flight job for ``ticker``, if any -- this is what
    makes ``POST /research/{symbol}/prepare`` idempotent: a second request
    while one is already running reuses it instead of starting a
    duplicate background task that would just race the first one."""
    return (
        db.query(ResearchPreparationJob)
        .filter(
            ResearchPreparationJob.ticker == ticker,
            ResearchPreparationJob.status == JobStatus.RUNNING,
        )
        .order_by(ResearchPreparationJob.started_at.desc())
        .first()
    )


_StepResult = tuple[StepStatus, str | None]


def _step_record(
    step: PreparationStep,
    status: StepStatus,
    detail: str | None,
    now: datetime,
    retryable: bool | None = None,
) -> dict[str, Any]:
    return {
        "step": step.value,
        "status": status.value,
        "detail": detail,
        "updated_at": now.isoformat(),
        # None for pending/running/done/skipped (the question doesn't
        # apply); True/False only set on a real failure, so the frontend
        # can tell "retrying now would plausibly help" (a transient
        # network/provider error) from "retrying now will just fail the
        # same way again" (e.g. Alpha Vantage's daily quota already spent
        # -- see providers/alpha_vantage.py::AlphaVantageError.rate_limited).
        "retryable": retryable,
    }


def _retryable_for_exception(exc: Exception) -> bool:
    from providers.alpha_vantage import AlphaVantageError

    if isinstance(exc, AlphaVantageError):
        return not exc.rate_limited
    return True


def _set_step(
    db: Session,
    job: ResearchPreparationJob,
    step: PreparationStep,
    status: StepStatus,
    detail: str | None,
    now: datetime,
    retryable: bool | None = None,
) -> None:
    steps = list(job.steps)
    for i, s in enumerate(steps):
        if s["step"] == step.value:
            steps[i] = _step_record(step, status, detail, now, retryable)
            break
    job.steps = steps
    # Pre-live hardening (2026-08-25): every real step transition is also
    # a real heartbeat -- this is the only signal services/research_
    # preparation_queue.py's stale-lease recovery has for "is the worker
    # behind this RUNNING row still alive", so it must land on the exact
    # same commit as the step update itself, not a separate best-effort
    # write that could be skipped. A row from the on-demand (Search page)
    # path updates this too, harmlessly -- nothing reads it there.
    job.heartbeat_at = datetime.now(UTC)
    db.add(job)
    db.commit()


def _fail_job(
    db: Session, job: ResearchPreparationJob, step: PreparationStep, exc: Exception
) -> ResearchPreparationJob:
    now = datetime.now(UTC)
    detail = str(exc)[:500]
    _set_step(
        db, job, step, StepStatus.FAILED, detail, now, retryable=_retryable_for_exception(exc)
    )
    job.status = JobStatus.FAILED
    job.error = f"{step.value}: {detail}"[:500]
    job.completed_at = now
    db.add(job)
    db.commit()
    log.error("%s: required step %s failed: %s", job.ticker, step.value, detail)
    return job


def _historical_earnings_last_updated(db: Session, company_id: int) -> datetime | None:
    return (
        db.query(func.max(EarningsResult.retrieved_at))
        .join(EarningsEvent, EarningsResult.earnings_event_id == EarningsEvent.id)
        .filter(EarningsEvent.company_id == company_id)
        .scalar()
    )


def _price_history_last_updated(db: Session, ticker: str) -> datetime | None:
    return db.query(func.max(PriceBar.retrieved_at)).filter(PriceBar.ticker == ticker).scalar()


def _sec_filings_last_updated(db: Session, company_id: int) -> datetime | None:
    """Freshness anchor for *this* step specifically -- scoped to the 10-K/
    10-Q filings it actually fetches (see _prepare_sec_filings), not every
    Filing row for the company. Without this scoping, real 8-K rows the
    historical-earnings step separately persists (for earnings-date
    matching, see ingestion.earnings_date_backfill) would falsely satisfy
    this step's freshness check on a brand-new company and permanently
    skip ever fetching its actual 10-K/10-Q filings -- a real bug caught
    live preparing a genuinely new ticker (COST) during Phase 14
    verification, not a hypothetical.
    """
    return (
        db.query(func.max(Filing.retrieved_at))
        .filter(
            Filing.company_id == company_id,
            Filing.filing_type.in_([FilingType.FORM_10K, FilingType.FORM_10Q]),
        )
        .scalar()
    )


def _earnings_estimates_last_updated(db: Session, company_id: int) -> datetime | None:
    return (
        db.query(func.max(EarningsEstimateSnapshot.retrieved_at))
        .filter(EarningsEstimateSnapshot.company_id == company_id)
        .scalar()
    )


def _options_chain_last_updated(db: Session, company_id: int) -> datetime | None:
    return (
        db.query(func.max(OptionsSnapshot.retrieved_at))
        .filter(OptionsSnapshot.company_id == company_id)
        .scalar()
    )


def _prepare_historical_earnings(
    db: Session, edgar: SECEdgarProvider, company: Company, as_of: datetime, force: bool = False
) -> _StepResult:
    if company.cik is None:
        return StepStatus.SKIPPED, "no CIK on record"
    status = assess_freshness(
        DataClass.HISTORICAL_EARNINGS, _historical_earnings_last_updated(db, company.id), as_of
    )
    if not force and not needs_refresh(status):
        return StepStatus.DONE, "already fresh, reused existing data"

    facts = edgar.get_company_facts(company.cik)
    eps_by_period = _index_by_period(facts.eps_diluted)
    revenue_by_period = _index_by_period(facts.revenues)
    updated = 0
    for (fiscal_year, fp), eps_value in sorted(eps_by_period.items()):
        fiscal_quarter = _FP_TO_QUARTER.get(fp)
        if fiscal_quarter is None:
            continue
        revenue_value = revenue_by_period.get((fiscal_year, fp))
        _upsert_earnings_event_and_result(
            db, company, fiscal_year, fiscal_quarter, eps_value, revenue_value
        )
        updated += 1
    db.commit()

    _backfill_period_end_dates(db, edgar, company)
    _backfill_earnings_dates(db, edgar, company)
    return StepStatus.DONE, f"{updated} reported quarters on record"


def _prepare_price_history(
    db: Session,
    market_data: MarketDataProvider,
    company: Company,
    as_of: datetime,
    force: bool = False,
) -> _StepResult:
    status = assess_freshness(
        DataClass.PRICE_HISTORY, _price_history_last_updated(db, company.ticker), as_of
    )
    if not force and not needs_refresh(status):
        return StepStatus.DONE, "already fresh, reused existing data"

    _ingest_price_bars(db, market_data, MARKET_TICKER)
    # SOXX is a semiconductor-sector benchmark -- only meaningful for the
    # companies actually in that sector. For everyone else, sector_return
    # honestly stays null rather than reusing a benchmark that doesn't fit.
    has_sector_benchmark = company.sector == "Semiconductors"
    if has_sector_benchmark:
        _ingest_price_bars(db, market_data, SECTOR_TICKER)
    count = _ingest_price_bars(db, market_data, company.ticker)

    ticker_bars = _load_close_series(db, company.ticker)
    market_bars = _load_close_series(db, MARKET_TICKER)
    sector_bars = _load_close_series(db, SECTOR_TICKER) if has_sector_benchmark else {}
    updated = _backfill_snapshots(db, company, ticker_bars, market_bars, sector_bars)
    return StepStatus.DONE, f"{count} daily bars, {updated} earnings-reaction snapshots"


def _prepare_earnings_estimates(
    db: Session,
    estimates: EarningsEstimatesProvider | None,
    company: Company,
    as_of: datetime,
    force: bool = False,
) -> _StepResult:
    status = assess_freshness(
        DataClass.EARNINGS_ESTIMATES, _earnings_estimates_last_updated(db, company.id), as_of
    )
    if not force and not needs_refresh(status):
        return StepStatus.DONE, "already fresh, reused existing data"
    # Calendar first (v4.0.2): the earnings calendar's date and consensus are
    # real data already on record; Alpha Vantage is only a fallback.
    row, note = collect_next_earnings_estimate_calendar_first(
        db, estimates, company, today=as_of.date()
    )
    if row is None:
        return StepStatus.SKIPPED, note
    return StepStatus.DONE, note


def _prepare_sec_filings(
    db: Session, edgar: SECEdgarProvider, company: Company, as_of: datetime, force: bool = False
) -> _StepResult:
    if company.cik is None:
        return StepStatus.SKIPPED, "no CIK on record"
    status = assess_freshness(
        DataClass.SEC_FILINGS, _sec_filings_last_updated(db, company.id), as_of
    )
    if not force and not needs_refresh(status):
        return StepStatus.DONE, "already fresh, reused existing data"
    filings = edgar.search_filings(company.cik, filing_types=["10-K", "10-Q"], limit=4)
    for meta in filings:
        _upsert_filing(db, company, meta)
    db.commit()
    return StepStatus.DONE, f"{len(filings)} recent 10-K/10-Q filings on record"


# The only filing types _prepare_sec_filings actually fetches and that
# generate_decision()'s own RAG evidence step (FilingsSearchTool, see
# services/decision_engine.py::_gather_evidence) queries for -- "business
# overview" and "risk factors" content lives in the 10-K/10-Q, not a
# press-release-style 8-K. Real bug this scoping fixes (pre-live
# hardening, 2026-08-25): _backfill_earnings_dates (ingestion/backfill_
# earnings_dates.py, called by the HISTORICAL_EARNINGS step just before
# this one) persists a real Filing row for every matched 8-K -- up to
# several dozen for an established company -- purely to record which
# filing confirmed each quarter's real earnings date. Before this fix,
# this step queried *every* Filing row for the company regardless of
# type, so it unintentionally fetched and embedded every one of those
# 8-Ks too, dominating this step's real cost (confirmed live: one
# company's preparation made ~30-45 real SEC document fetches here alone,
# vs. the 4 filings _prepare_sec_filings itself actually intends).
_EMBEDDABLE_FILING_TYPES = (FilingType.FORM_10K, FilingType.FORM_10Q)


def _prepare_filing_embeddings(
    db: Session, edgar: SECEdgarProvider, embedder: EmbeddingProvider, company: Company
) -> _StepResult:
    company_filings = (
        db.query(Filing)
        .filter(Filing.company_id == company.id, Filing.filing_type.in_(_EMBEDDABLE_FILING_TYPES))
        .all()
    )
    if not company_filings:
        return StepStatus.SKIPPED, "no filings on record yet"
    chunked_filing_ids = {
        row[0]
        for row in db.query(DocumentChunk.filing_id)
        .filter(DocumentChunk.company_id == company.id)
        .distinct()
        .all()
    }
    targets = [f for f in company_filings if f.id not in chunked_filing_ids]
    if not targets:
        return StepStatus.DONE, "already fresh, reused existing data"
    total_chunks = 0
    for filing in targets:
        total_chunks += ingest_filing(db, edgar, embedder, filing)
    return StepStatus.DONE, f"{total_chunks} chunks across {len(targets)} filings"


def _prepare_options_chain(
    db: Session,
    options: OptionsDataProvider | None,
    company: Company,
    as_of: datetime,
    force: bool = False,
) -> _StepResult:
    """Collects a real options-chain snapshot for ``company`` -- earnings-
    anchored when a reliable upcoming earnings date is on record,
    general/current (nearest practical expiration, honestly labeled as not
    earnings-anchored) otherwise. Never skips collection just because no
    provider (Alpha Vantage or a manual override -- see
    services/market_expectations.py) has published a date yet: an options
    chain and an earnings date are two independently real things, and one
    missing must not silently block the other. See
    docs/ibkr_integration.md and analytics/options/implied_move.py.
    """
    if options is None:
        return StepStatus.SKIPPED, "no options-data provider configured"
    estimate = get_latest_earnings_estimate(db, company.id)
    earnings_date = (
        estimate.estimated_report_date
        if estimate is not None and estimate.estimated_report_date is not None
        else None
    )

    status = assess_freshness(
        DataClass.OPTIONS_CHAIN, _options_chain_last_updated(db, company.id), as_of
    )
    if not force and not needs_refresh(status):
        return StepStatus.DONE, "already fresh, reused existing data"

    snapshots = collect_options_snapshot_now(db, options, company, earnings_date, as_of)
    if not snapshots:
        return StepStatus.SKIPPED, "provider returned no contracts"
    compute_and_persist_volatility_snapshot(db, company, earnings_date)
    anchor_note = (
        f"earnings-anchored to {earnings_date}" if earnings_date is not None else "general/current"
    )
    priceable_count = sum(
        1 for s in snapshots if s.bid is not None or s.ask is not None or s.last_price is not None
    )
    # Phase 14.12: contract *discovery* succeeding is not the same claim as
    # pricing being usable -- a step that reports "22 option quotes" reads
    # as an unqualified success even when zero of them have a bid/ask/last
    # (real, observed: pre-market IBKR frozen quotes). Always state both
    # numbers so this step can never be mistaken for "strategies are ready."
    return (
        StepStatus.DONE,
        f"{len(snapshots)} contracts, {priceable_count} priceable ({anchor_note})",
    )


def _prepare_earnings_analysis(db: Session, company: Company) -> _StepResult:
    comparisons = get_implied_vs_realized_moves(db, company.id)
    if comparisons:
        return StepStatus.DONE, f"{len(comparisons)} implied-vs-realized move comparisons available"
    has_history = (
        db.query(PriceReaction.id)
        .join(EarningsEvent, PriceReaction.earnings_event_id == EarningsEvent.id)
        .filter(EarningsEvent.company_id == company.id)
        .first()
        is not None
    )
    if has_history:
        return StepStatus.DONE, "historical price-reaction data available"
    return StepStatus.SKIPPED, "not enough data yet for earnings analysis"


#: A thesis older than this is regenerated by the preparation pipeline;
#: nightly preparation therefore refreshes each upcoming company at most
#: once a week, and the V4 gate always sees a thesis grounded in research
#: no older than a week at the legal decision moment.
THESIS_FRESHNESS_DAYS = 7


def _prepare_ai_thesis(
    db: Session,
    providers: ResearchProviders,
    company: Company,
    as_of: datetime,
    force: bool,
) -> _StepResult:
    """Final preparation step (V4-only reset, 2026-09-02): make the company
    V4-decision-ready by ensuring a fresh AI thesis exists. Uses the same
    generation and persistence path as the Research page's own "Generate
    thesis" action, with the general-purpose model. Never the V4
    DecisionView model, and never a decision of any kind."""
    from models.ai_thesis_version import AIThesisVersion  # noqa: PLC0415
    from services.earnings_thesis import generate_earnings_thesis  # noqa: PLC0415
    from services.research_history import ThesisProvenance, persist_thesis_version  # noqa: PLC0415

    latest = (
        db.query(AIThesisVersion)
        .filter_by(company_id=company.id)
        .order_by(AIThesisVersion.created_at.desc())
        .first()
    )
    if latest is not None and not force:
        age = as_of - latest.created_at
        if age.total_seconds() < THESIS_FRESHNESS_DAYS * 86400:
            return StepStatus.DONE, f"fresh thesis on record ({age.days}d old, id={latest.id})"
    if providers.llm is None:
        return StepStatus.SKIPPED, "no LLM configured -- thesis not generated"
    result = generate_earnings_thesis(db, providers.llm, providers.embedder, company)
    row = persist_thesis_version(
        db,
        company=company,
        provider=providers.llm.name,
        result=result,
        provenance=ThesisProvenance(
            estimate_snapshot_id=result.estimate_snapshot_id,
            volatility_snapshot_id=result.volatility_snapshot_id,
        ),
    )
    return StepStatus.DONE, f"thesis generated (id={row.id}, model={result.model})"


def prepare_company_research(
    db: Session,
    symbol: str,
    providers: ResearchProviders,
    now: datetime | None = None,
    force: bool = False,
    existing_job: ResearchPreparationJob | None = None,
) -> ResearchPreparationJob:
    """Runs the full on-demand preparation pipeline for ``symbol`` and
    returns the finished (or failed) job row. Raises ``UnsupportedSymbolError``
    before creating any job row at all when the symbol itself can't be
    honestly researched (see services.symbol_resolution) -- an unsupported
    ticker never gets a job, a company, or any other trace in the database.

    ``force=True`` (the API layer's explicit "Refresh" action, as opposed to
    "Prepare") bypasses every step's freshness check and always re-fetches
    -- otherwise this call is identical to a normal freshness-gated
    preparation run, including still deduplicating same-day snapshots (see
    services.options_analytics.collect_options_snapshot_now).

    ``existing_job`` (pre-live hardening, 2026-08-25): when given, this
    call updates and reuses THAT row instead of creating a new one --
    the durable-queue worker (services/research_preparation_queue.py,
    workers/research_preparation_worker.py) already has a claimed queue
    row (with its own earnings_calendar_event_id, attempt_count, lease
    columns) and needs the step-level detail written onto that SAME row
    so Operations can show live progress for one real, single, coherent
    row per queued candidate -- not a second, disconnected row. The
    on-demand Search-page path never passes this (stays None), so its
    behavior -- always a fresh row -- is completely unchanged.
    """
    as_of = now if now is not None else datetime.now(UTC)
    resolution: SymbolResolution = resolve_symbol(db, providers.edgar, symbol)
    if not resolution.supported:
        raise UnsupportedSymbolError(resolution.reason or f"{symbol!r} is not supported")

    if existing_job is not None:
        job = existing_job
        job.ticker = resolution.ticker
        job.company_id = resolution.existing_company.id if resolution.existing_company else None
        job.status = JobStatus.RUNNING
        job.steps = [_step_record(s, StepStatus.PENDING, None, as_of) for s in PreparationStep]
        job.completed_at = None
        job.error = None
        job.heartbeat_at = as_of
    else:
        job = ResearchPreparationJob(
            ticker=resolution.ticker,
            company_id=resolution.existing_company.id if resolution.existing_company else None,
            status=JobStatus.RUNNING,
            steps=[_step_record(s, StepStatus.PENDING, None, as_of) for s in PreparationStep],
            started_at=as_of,
        )
    db.add(job)
    db.commit()

    try:
        if resolution.existing_company is not None:
            company = resolution.existing_company
            _set_step(
                db,
                job,
                PreparationStep.COMPANY_IDENTIFIED,
                StepStatus.DONE,
                "already on record",
                as_of,
            )
        else:
            assert resolution.cik is not None  # guaranteed by resolve_symbol when supported and new
            facts = providers.edgar.get_company_facts(resolution.cik)
            company = _upsert_company(
                db, resolution.ticker, resolution.cik, facts.entity_name or resolution.ticker
            )
            db.commit()
            _set_step(
                db,
                job,
                PreparationStep.COMPANY_IDENTIFIED,
                StepStatus.DONE,
                f"created new company record ({company.name})",
                as_of,
            )
    except Exception as exc:  # noqa: BLE001 -- boundary: any failure here must degrade the job, never crash the caller
        return _fail_job(db, job, PreparationStep.COMPANY_IDENTIFIED, exc)

    job.company_id = company.id
    db.add(job)
    db.commit()

    steps: list[tuple[PreparationStep, bool, Callable[[], _StepResult]]] = [
        (
            PreparationStep.HISTORICAL_EARNINGS,
            True,
            lambda: _prepare_historical_earnings(db, providers.edgar, company, as_of, force),
        ),
        (
            PreparationStep.PRICE_HISTORY,
            True,
            lambda: _prepare_price_history(db, providers.market_data, company, as_of, force),
        ),
        (
            PreparationStep.EARNINGS_ESTIMATES,
            False,
            lambda: _prepare_earnings_estimates(db, providers.estimates, company, as_of, force),
        ),
        (
            PreparationStep.SEC_FILINGS,
            False,
            lambda: _prepare_sec_filings(db, providers.edgar, company, as_of, force),
        ),
        (
            PreparationStep.FILING_EMBEDDINGS,
            False,
            lambda: _prepare_filing_embeddings(db, providers.edgar, providers.embedder, company),
        ),
        (
            PreparationStep.OPTIONS_CHAIN,
            False,
            lambda: _prepare_options_chain(db, providers.options, company, as_of, force),
        ),
        (
            PreparationStep.EARNINGS_ANALYSIS,
            False,
            lambda: _prepare_earnings_analysis(db, company),
        ),
        (
            PreparationStep.AI_THESIS,
            False,
            lambda: _prepare_ai_thesis(db, providers, company, as_of, force),
        ),
    ]

    warnings = False
    for step, required, fn in steps:
        # Pre-live hardening (2026-08-25): a real, persisted "this step is
        # in flight right now" marker, overwritten by the real outcome the
        # instant fn() returns -- purely additive (every existing test
        # only ever inspects the job's FINAL steps, unaffected), and the
        # only way Operations can show real live progress ("Stage: SEC
        # filings") for a run still in progress rather than only after
        # the fact.
        _set_step(db, job, step, StepStatus.RUNNING, None, datetime.now(UTC))
        try:
            status, detail = fn()
        except Exception as exc:  # noqa: BLE001 -- boundary: one step's real failure must not crash the run
            if required:
                return _fail_job(db, job, step, exc)
            detail = str(exc)[:500]
            _set_step(
                db,
                job,
                step,
                StepStatus.FAILED,
                detail,
                datetime.now(UTC),
                retryable=_retryable_for_exception(exc),
            )
            warnings = True
            log.warning("%s: optional step %s failed: %s", resolution.ticker, step.value, detail)
            continue
        _set_step(db, job, step, status, detail, datetime.now(UTC))
        if status == StepStatus.FAILED:
            warnings = True

    job.status = JobStatus.COMPLETED_WITH_WARNINGS if warnings else JobStatus.COMPLETED
    job.completed_at = datetime.now(UTC)
    db.add(job)
    db.commit()
    return job


assert REQUIRED_STEPS == {
    PreparationStep.COMPANY_IDENTIFIED,
    PreparationStep.HISTORICAL_EARNINGS,
    PreparationStep.PRICE_HISTORY,
}, "prepare_company_research's hardcoded `required` flags above must match REQUIRED_STEPS"
