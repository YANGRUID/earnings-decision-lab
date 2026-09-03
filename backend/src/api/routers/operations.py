"""Live Operations Monitor -- V4-only read-only aggregation endpoints (V4-only
reset, 2026-09-02). See services/operations.py. No mutation endpoint of any
kind; the dev-only /admin/* triggers remain the only manual controls.
"""

from fastapi import APIRouter

from api.deps import DbSession, Scheduler, TwsHealthProbeDep, TwsProviderDep
from core.config import get_settings
from schemas.api import (
    ForwardWindowResponse,
    MarketClockResponse,
    OperationsEventsResponse,
    OperationsFailuresResponse,
    OperationsJobsResponse,
    OperationsSummaryResponse,
    PreflightReadinessResponse,
    PreparationProgressResponse,
    ResearchReadinessResponse,
    SystemHealthResponse,
    V4TodaySummaryResponse,
)
from services.operations import (
    compute_forward_window,
    compute_research_readiness,
    compute_today_summary,
    detect_missed_job_alerts,
    forward_pipeline,
    get_market_clock,
    get_preflight_readiness,
    get_preparation_progress,
    get_recent_failures,
    get_scheduler_jobs,
    get_system_health,
    get_v4_pipeline,
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
    pipeline = get_v4_pipeline(db)
    readiness = compute_research_readiness(pipeline)
    today = compute_today_summary(db, pipeline)
    jobs = get_scheduler_jobs(db, scheduler_status)
    _, staleness = detect_missed_job_alerts(db, jobs, pipeline)
    preflight = get_preflight_readiness(db, health, readiness, staleness)
    return OperationsSummaryResponse(
        health=SystemHealthResponse.model_validate(health),
        today=V4TodaySummaryResponse.model_validate(today),
        readiness=ResearchReadinessResponse.model_validate(readiness),
        preflight=PreflightReadinessResponse.model_validate(preflight),
        market_clock=MarketClockResponse.model_validate(get_market_clock(scheduler_status)),
        staleness=staleness,  # type: ignore[arg-type]
        forward_window=ForwardWindowResponse.model_validate(compute_forward_window(db, pipeline)),
    )


@router.get("/events", response_model=OperationsEventsResponse)
def get_operations_events(db: DbSession, include_past: bool = False) -> OperationsEventsResponse:
    """The V4 pipeline. By default forward-only: windows still open or ahead
    plus every event with real V4 evidence; ``include_past=true`` returns the
    complete monitoring view."""
    pipeline = get_v4_pipeline(db)
    if not include_past:
        pipeline = forward_pipeline(pipeline)
    return OperationsEventsResponse(events=pipeline)  # type: ignore[arg-type]


@router.get("/jobs", response_model=OperationsJobsResponse)
def get_operations_jobs(db: DbSession, scheduler: Scheduler) -> OperationsJobsResponse:
    scheduler_status = get_scheduler_status(scheduler)
    return OperationsJobsResponse(jobs=get_scheduler_jobs(db, scheduler_status))  # type: ignore[arg-type]


@router.get("/failures", response_model=OperationsFailuresResponse)
def get_operations_failures(db: DbSession, scheduler: Scheduler) -> OperationsFailuresResponse:
    scheduler_status = get_scheduler_status(scheduler)
    pipeline = get_v4_pipeline(db)
    jobs = get_scheduler_jobs(db, scheduler_status)
    failures = get_recent_failures(db)
    alerts, _ = detect_missed_job_alerts(db, jobs, pipeline)
    combined = sorted([*alerts, *failures], key=lambda f: f.occurred_at, reverse=True)
    return OperationsFailuresResponse(failures=combined)  # type: ignore[arg-type]


@router.get("/preparation-progress", response_model=PreparationProgressResponse)
def get_operations_preparation_progress(db: DbSession) -> PreparationProgressResponse:
    return PreparationProgressResponse.model_validate(get_preparation_progress(db))


@router.get("/health", response_model=SystemHealthResponse)
def get_operations_health(
    db: DbSession,
    scheduler: Scheduler,
    tws_health_probe: TwsHealthProbeDep,
    tws_provider: TwsProviderDep,
) -> SystemHealthResponse:
    settings = get_settings()
    scheduler_status = get_scheduler_status(scheduler)
    health = get_system_health(db, settings, scheduler_status, tws_health_probe, tws_provider)
    return SystemHealthResponse.model_validate(health)
