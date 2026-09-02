"""Live Operations Monitor -- read-only aggregation endpoints. See
services/operations.py's own module docstring for the full design
rationale (no live IBKR/EarningsAPI/LLM call is ever made just to answer
one of these GETs; every value is a real, already-persisted row or a
pure computation over one).

Deliberately no mutation endpoint of any kind -- this router exists
purely for observability during real, live forward testing. Force-
decision/force-entry/force-settlement/override-eligibility controls are
explicitly out of scope (see the brief this router was built against);
the existing dev-only /admin/* trigger endpoints remain the only way to
manually kick a real job, and this router never wraps or exposes them.
"""

from fastapi import APIRouter

from api.deps import DbSession, Scheduler, TwsHealthProbeDep, TwsProviderDep
from api.exceptions import NotFoundError
from core.config import get_settings
from schemas.api import (
    ExecutionSummaryResponse,
    MarketClockResponse,
    OperationsEventsResponse,
    OperationsFailuresResponse,
    OperationsJobsResponse,
    OperationsSummaryResponse,
    PreflightReadinessResponse,
    PreparationProgressResponse,
    QuoteDiagnosticsResponse,
    QuoteDiagnosticsSummaryResponse,
    SystemHealthResponse,
    TodaysOfficialRunResponse,
)
from services.operations import (
    FailureEntry,
    compute_execution_summary,
    detect_missed_job_alerts,
    get_market_clock,
    get_preflight_readiness,
    get_preparation_progress,
    get_recent_failures,
    get_scheduler_jobs,
    get_system_health,
    get_todays_official_run,
    get_todays_pipeline,
)
from services.quote_diagnostics import (
    detect_missing_quote_telemetry,
    get_entry_quote_diagnostics,
    get_quote_diagnostics_summary,
    get_settlement_quote_diagnostics,
)
from services.scheduler import get_scheduler_status

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get("/summary", response_model=OperationsSummaryResponse)
def get_operations_summary(
    db: DbSession,
    scheduler: Scheduler,
    tws_health_probe: TwsHealthProbeDep,
    tws_provider: TwsProviderDep,
) -> OperationsSummaryResponse:
    settings = get_settings()
    scheduler_status = get_scheduler_status(scheduler)
    health = get_system_health(db, settings, scheduler_status, tws_health_probe, tws_provider)
    pipeline_events = get_todays_pipeline(db)
    execution_summary = compute_execution_summary(pipeline_events)
    official_run = get_todays_official_run(db)
    preflight = get_preflight_readiness(db, health)
    market_clock = get_market_clock(scheduler_status)
    return OperationsSummaryResponse(
        health=SystemHealthResponse.model_validate(health),
        execution_summary=ExecutionSummaryResponse.model_validate(execution_summary),
        official_run=TodaysOfficialRunResponse.model_validate(official_run),
        preflight=PreflightReadinessResponse.model_validate(preflight),
        market_clock=MarketClockResponse.model_validate(market_clock),
    )


@router.get("/events", response_model=OperationsEventsResponse)
def get_operations_events(db: DbSession) -> OperationsEventsResponse:
    events = get_todays_pipeline(db)
    return OperationsEventsResponse(events=events)  # type: ignore[arg-type]


@router.get("/jobs", response_model=OperationsJobsResponse)
def get_operations_jobs(db: DbSession, scheduler: Scheduler) -> OperationsJobsResponse:
    scheduler_status = get_scheduler_status(scheduler)
    jobs = get_scheduler_jobs(db, scheduler_status)
    return OperationsJobsResponse(jobs=jobs)  # type: ignore[arg-type]


@router.get("/failures", response_model=OperationsFailuresResponse)
def get_operations_failures(
    db: DbSession,
    scheduler: Scheduler,
    tws_health_probe: TwsHealthProbeDep,
    tws_provider: TwsProviderDep,
) -> OperationsFailuresResponse:
    settings = get_settings()
    scheduler_status = get_scheduler_status(scheduler)
    health = get_system_health(db, settings, scheduler_status, tws_health_probe, tws_provider)
    pipeline_events = get_todays_pipeline(db)
    scheduler_jobs = get_scheduler_jobs(db, scheduler_status)
    failures = get_recent_failures(db)
    missed_job_alerts = detect_missed_job_alerts(
        scheduler_jobs,
        pipeline_events,
        health,
        forward_test_activation_at=settings.forward_test_activation_at,
    )
    # Phase 4 quote-observability hardening (2026-08-26), Section 19 --
    # validates the telemetry wiring prospectively: a real capture that
    # reached the provider call but somehow left zero QuoteAcquisition
    # Attempt rows would otherwise be silently invisible.
    telemetry_alerts = [
        FailureEntry(
            occurred_at=a.occurred_at,
            symbol=a.ticker,
            stage=f"{a.capture_attempt_type}_capture",
            category="missing_quote_telemetry",
            explanation=(
                f"{a.capture_attempt_type.capitalize()} capture attempt "
                f"{a.entry_capture_attempt_id or a.settlement_capture_attempt_id} reached the "
                "provider call but has zero QuoteAcquisitionAttempt rows on record"
            ),
            detail=a.capture_error,
            retryability="NOT_RETRYABLE",
        )
        for a in detect_missing_quote_telemetry(db)
    ]
    combined = sorted(
        [*failures, *missed_job_alerts, *telemetry_alerts],
        key=lambda f: f.occurred_at,
        reverse=True,
    )
    return OperationsFailuresResponse(failures=combined)  # type: ignore[arg-type]


@router.get("/preparation-progress", response_model=PreparationProgressResponse)
def get_operations_preparation_progress(db: DbSession) -> PreparationProgressResponse:
    """Real, live state of the durable research-preparation queue --
    see services/operations.py::get_preparation_progress. An honest
    ``worker_active: false`` when no row is currently claimed."""
    progress = get_preparation_progress(db)
    return PreparationProgressResponse.model_validate(progress)


@router.get("/health", response_model=SystemHealthResponse)
def get_operations_health(
    db: DbSession,
    scheduler: Scheduler,
    tws_health_probe: TwsHealthProbeDep,
    tws_provider: TwsProviderDep,
) -> SystemHealthResponse:
    """A dedicated, lighter endpoint for just the top-of-page health
    cards (Section 2) -- the same real data /operations/summary already
    includes, for a caller that only wants to poll the health strip
    without also re-fetching today's full pipeline/execution summary."""
    settings = get_settings()
    scheduler_status = get_scheduler_status(scheduler)
    health = get_system_health(db, settings, scheduler_status, tws_health_probe, tws_provider)
    return SystemHealthResponse.model_validate(health)


@router.get(
    "/quote-diagnostics/entry/{entry_capture_attempt_id}",
    response_model=QuoteDiagnosticsResponse,
)
def get_entry_quote_diagnostics_endpoint(
    entry_capture_attempt_id: int, db: DbSession
) -> QuoteDiagnosticsResponse:
    """Phase 4 quote-observability hardening (2026-08-26), Section 13 --
    the real, structured per-leg polling history for one EntryCaptureAttempt
    (expandable diagnostics, not part of the main pipeline view). 404 when
    no QuoteAcquisitionAttempt rows exist for this attempt -- a legacy
    (Aug 25) capture predating this table, or one whose own writer never
    ran, never a fabricated empty-but-present response."""
    diagnostics = get_entry_quote_diagnostics(db, entry_capture_attempt_id)
    if diagnostics is None:
        raise NotFoundError(
            f"no quote-acquisition telemetry on record for entry capture attempt "
            f"{entry_capture_attempt_id!r}"
        )
    return QuoteDiagnosticsResponse.model_validate(diagnostics)


@router.get(
    "/quote-diagnostics/settlement/{settlement_capture_attempt_id}",
    response_model=QuoteDiagnosticsResponse,
)
def get_settlement_quote_diagnostics_endpoint(
    settlement_capture_attempt_id: int, db: DbSession
) -> QuoteDiagnosticsResponse:
    """Settlement's mirror of the entry endpoint above."""
    diagnostics = get_settlement_quote_diagnostics(db, settlement_capture_attempt_id)
    if diagnostics is None:
        raise NotFoundError(
            f"no quote-acquisition telemetry on record for settlement capture attempt "
            f"{settlement_capture_attempt_id!r}"
        )
    return QuoteDiagnosticsResponse.model_validate(diagnostics)


@router.get("/quote-diagnostics/summary", response_model=QuoteDiagnosticsSummaryResponse)
def get_quote_diagnostics_summary_endpoint(db: DbSession) -> QuoteDiagnosticsSummaryResponse:
    """Section 14 -- a compact, bounded aggregate over recent quote-
    acquisition telemetry (contracts requested/resolved, attempt counts,
    failure-category counts). Diagnostic statistics about the acquisition
    process, never a trading-performance metric."""
    summary = get_quote_diagnostics_summary(db)
    return QuoteDiagnosticsSummaryResponse.model_validate(summary)
