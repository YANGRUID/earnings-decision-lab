"""Live Operations Monitor -- V4-only read models (V4-only reset, 2026-09-02).

Everything here is a real, already-persisted row or a pure computation over
one. No live IBKR / EarningsAPI / LLM call is ever made to answer a GET:
TWS health comes from the shared, long-lived health probe the app lifespan
owns, never a new socket.

Domains reported:

* system health (TWS, calendar, AI provider + the V4 DecisionView model,
  scheduler, database) and the V4 forward-test health block;
* research readiness KPIs for the upcoming window, so it is obvious
  BEFORE 15:30 ET whether tomorrow's pipeline is under-prepared;
* the V4 pipeline: one row per calendar event in the window with a
  V4-specific lifecycle state, next action and timeline;
* scheduler job monitor (every registered job), research-preparation
  progress, the V4 failure centre, missed/stale-run alerts and the
  pre-flight readiness for today's 15:30 ET window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from analytics.decision.v4_methodology import V4_METHODOLOGY
from analytics.decision_timing_policy import V4_ACTIVE_TIMING_POLICY
from analytics.earnings_timing import EarningsEntryExitSchedule, compute_entry_exit_schedule
from analytics.forward_windows import (
    DECISION_DEADLINE_ET,
    EARLY_CAPTURE_TOLERANCE,
    LATE_CUTOFF_GRACE,
    announcement_session,
)
from analytics.market_session import EASTERN, get_market_session
from core.config import Settings
from models.ai_thesis_version import AIThesisVersion
from models.company import Company
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import ProviderHealthStatus
from models.provider_health_event import ProviderHealthEvent
from models.research_preparation_job import (
    STEP_LABELS,
    JobStatus,
    PreparationStep,
    ResearchPreparationJob,
)
from models.scheduler_run import SchedulerRun
from models.v4_shadow import (
    V4ForwardWindowTelemetry,
    V4ShadowConfigEntry,
    V4ShadowConfigSettlement,
    V4ShadowDecision,
    V4ShadowRunEvent,
)
from providers.ibkr_client import IBKRClient
from providers.ibkr_tws_health import TwsHealthProbe
from providers.ibkr_tws_options import IBKRTWSProvider
from services.earnings_eligibility import MIN_MARKET_CAP, US_COUNTRY_CODE
from services.provider_status import get_provider_dashboard
from services.research_orchestration import THESIS_FRESHNESS_DAYS
from services.research_preparation_queue import count_queue_depth
from services.scheduler import (
    CALENDAR_SYNC_JOB_ID,
    EARNINGS_RESEARCH_PREPARATION_JOB_ID,
    IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
    RESEARCH_PREPARATION_STARTUP_CATCHUP_JOB_ID,
    RESEARCH_READINESS_CATCHUP_JOB_ID,
    SchedulerStatus,
)
from services.system_status import IbkrStatus, get_ibkr_status, get_tws_status
from services.v4_shadow_scheduler import (
    V4_FORWARD_WINDOW_JOB_ID,
    V4_SHADOW_DECISION_JOB_ID,
    V4_SHADOW_SETTLEMENT_JOB_ID,
)

log = logging.getLogger("services.operations")

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

PIPELINE_WINDOW_DAYS = 7
PIPELINE_LOOKBACK_DAYS = 2

STATE_CALENDAR_DISCOVERED = "CALENDAR_DISCOVERED"
STATE_BUSINESS_INELIGIBLE = "BUSINESS_INELIGIBLE"
STATE_COMPANY_RESOLUTION_FAILED = "COMPANY_RESOLUTION_FAILED"
STATE_RESEARCH_QUEUED = "RESEARCH_QUEUED"
STATE_RESEARCH_RUNNING = "RESEARCH_RUNNING"
STATE_RESEARCH_FAILED = "RESEARCH_FAILED"
STATE_RESEARCH_READY = "RESEARCH_READY"
STATE_WAITING_DECISION = "WAITING_DECISION"
STATE_RESEARCH_NOT_READY = "RESEARCH_NOT_READY"
STATE_DECISION_WINDOW_MISSED = "DECISION_WINDOW_MISSED"
STATE_DEADLINE_SKIPPED = "DEADLINE_SKIPPED"
STATE_DECISION_FAILED = "DECISION_FAILED"
STATE_NO_ACTION = "NO_ACTION"
STATE_ENTRY_OBSERVED = "ENTRY_OBSERVED"
STATE_ENTRY_FAILED = "ENTRY_FAILED"
STATE_WAITING_SETTLEMENT = "WAITING_SETTLEMENT"
STATE_SETTLED = "SETTLED"
STATE_SETTLEMENT_FAILED = "SETTLEMENT_FAILED"

_HEALTHY = "green"
_DEGRADED = "yellow"
_FAILED = "red"
_NOT_APPLICABLE = "gray"

RETRYABLE = "RETRYABLE"
NOT_RETRYABLE = "NOT_RETRYABLE"
WINDOW_MISSED = "WINDOW_MISSED"

# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IbkrHealth:
    state: str
    gateway_reachable: bool
    authenticated: bool
    connected: bool
    live_account: bool | None
    market_data_quality: str | None
    last_heartbeat_at: datetime | None
    last_error: str | None
    provider: str


@dataclass(frozen=True)
class EarningsCalendarHealth:
    state: str
    active_provider: str | None
    fallback_provider: str | None
    last_successful_sync_at: datetime | None
    events_received: int | None
    last_error: str | None
    next_scheduled_sync_at: datetime | None


@dataclass(frozen=True)
class AiProviderHealth:
    state: str
    provider: str
    configured: bool
    last_successful_generation_at: datetime | None
    last_error: str | None
    decision_view_model: str | None = None
    decision_view_thinking: str | None = None
    decision_view_reasoning_effort: str | None = None
    decision_view_max_tokens: int | None = None
    decision_view_config_error: str | None = None


@dataclass(frozen=True)
class SchedulerHealth:
    state: str
    running: bool
    registered_job_count: int
    last_activity_at: datetime | None
    next_activity_at: datetime | None


@dataclass(frozen=True)
class DatabaseHealth:
    state: str
    backend_healthy: bool
    database_healthy: bool
    migration_head: str | None


@dataclass(frozen=True)
class V4ForwardHealth:
    """The V4 forward test's own health block."""

    state: str
    enabled: bool
    decisions_today: int
    ranked_today: int
    no_action_today: int
    failed_today: int
    entry_observations_failed_today: int
    settlements_due: int
    settlements_complete: int
    last_run_at: datetime | None
    engine_version: str | None
    decision_time_et: str
    settlement_time_et: str
    timing_policy_version: str
    note: str


@dataclass(frozen=True)
class SystemHealth:
    ibkr: IbkrHealth
    earnings_calendar: EarningsCalendarHealth
    ai_provider: AiProviderHealth
    scheduler: SchedulerHealth
    database: DatabaseHealth
    v4_shadow: V4ForwardHealth | None = None


_LIVE_ACCOUNT_CACHE_TTL = timedelta(minutes=5)
_live_account_cache: tuple[datetime, bool | None] | None = None


def _is_live_ibkr_account(settings: Settings, ibkr_status: IbkrStatus) -> bool | None:
    """Web-gateway only (legacy transport): a cached, read-only isPaper check."""
    global _live_account_cache
    if not ibkr_status.authenticated or not ibkr_status.connected:
        return None
    now = datetime.now(UTC)
    if _live_account_cache is not None:
        cached_at, cached_value = _live_account_cache
        if now - cached_at < _LIVE_ACCOUNT_CACHE_TTL:
            return cached_value
    try:
        client = IBKRClient(base_url=settings.ibkr_base_url)
        data = client.get("/iserver/accounts").json()
        is_paper = data.get("isPaper")
        result = None if is_paper is None else not bool(is_paper)
    except Exception:  # noqa: BLE001 -- unknown, never guessed
        return None
    _live_account_cache = (now, result)
    return result


_HEALTHY_PROVIDER_STATUSES = (ProviderHealthStatus.CONNECTED,)


def _last_provider_error(db: Session, provider: str, domain: str) -> ProviderHealthEvent | None:
    """The most recent FAILED health event for a provider -- a success row
    (which may carry a provenance note in ``detail``) is never an error."""
    return (
        db.query(ProviderHealthEvent)
        .filter(
            ProviderHealthEvent.provider == provider,
            ProviderHealthEvent.domain == domain,
            ProviderHealthEvent.status.notin_(_HEALTHY_PROVIDER_STATUSES),
        )
        .order_by(ProviderHealthEvent.occurred_at.desc())
        .first()
    )


def _latest_successful_run_started_at(db: Session) -> datetime | None:
    return (
        db.query(func.max(SchedulerRun.started_at))
        .filter(SchedulerRun.status == "success")
        .scalar()
    )


def _latest_scheduler_run(db: Session, job_id: str) -> SchedulerRun | None:
    return (
        db.query(SchedulerRun)
        .filter(SchedulerRun.job_id == job_id)
        .order_by(SchedulerRun.started_at.desc())
        .first()
    )


def _latest_successful_scheduler_run(db: Session, job_id: str) -> SchedulerRun | None:
    return (
        db.query(SchedulerRun)
        .filter(SchedulerRun.job_id == job_id, SchedulerRun.status == "success")
        .order_by(SchedulerRun.started_at.desc())
        .first()
    )


def _get_database_health(db: Session) -> DatabaseHealth:
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 -- the health check IS the failure signal
        return DatabaseHealth(
            state=_FAILED, backend_healthy=True, database_healthy=False, migration_head=None
        )
    try:
        migration_head = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001
        migration_head = None
    return DatabaseHealth(
        state=_HEALTHY, backend_healthy=True, database_healthy=True, migration_head=migration_head
    )


def get_v4_shadow_health(db: Session, settings: Settings) -> V4ForwardHealth:
    policy = V4_ACTIVE_TIMING_POLICY
    if not settings.v4_shadow_enabled:
        return V4ForwardHealth(
            state=_NOT_APPLICABLE,
            enabled=False,
            decisions_today=0,
            ranked_today=0,
            no_action_today=0,
            failed_today=0,
            entry_observations_failed_today=0,
            settlements_due=0,
            settlements_complete=0,
            last_run_at=None,
            engine_version=None,
            decision_time_et=policy.entry_time.strftime("%H:%M"),
            settlement_time_et=policy.exit_time.strftime("%H:%M"),
            timing_policy_version=policy.version,
            note="V4 forward test is DISABLED (V4_SHADOW_ENABLED=false)",
        )
    day_start = datetime.now(UTC) - timedelta(hours=24)
    base = db.query(V4ShadowDecision).filter(V4ShadowDecision.generated_at >= day_start)
    decisions_today = base.count()
    ranked = base.filter(V4ShadowDecision.status == "RANKED").count()
    no_action = base.filter(V4ShadowDecision.status == "NO_ACTION").count()
    failed = base.filter(V4ShadowDecision.status == "FAILED").count()
    entry_failed = (
        db.query(V4ShadowConfigEntry)
        .filter(
            V4ShadowConfigEntry.status != "OBSERVED",
            V4ShadowConfigEntry.observed_at >= day_start,
        )
        .count()
    )
    settled_ids = db.query(V4ShadowConfigSettlement.shadow_config_result_id)
    settlements_due = (
        db.query(V4ShadowConfigEntry)
        .filter(V4ShadowConfigEntry.status == "OBSERVED")
        .filter(~V4ShadowConfigEntry.shadow_config_result_id.in_(settled_ids))
        .count()
    )
    settled = db.query(V4ShadowConfigSettlement).filter_by(status="SETTLED").count()
    last_run = db.query(func.max(V4ShadowDecision.generated_at)).scalar()
    return V4ForwardHealth(
        state=_DEGRADED if (failed or entry_failed) else _HEALTHY,
        enabled=True,
        decisions_today=decisions_today,
        ranked_today=ranked,
        no_action_today=no_action,
        failed_today=failed,
        entry_observations_failed_today=entry_failed,
        settlements_due=settlements_due,
        settlements_complete=settled,
        last_run_at=last_run,
        engine_version=V4_METHODOLOGY.engine_version,
        decision_time_et=policy.entry_time.strftime("%H:%M"),
        settlement_time_et=policy.exit_time.strftime("%H:%M"),
        timing_policy_version=policy.version,
        note=(
            "V4 forward test: no-order evidence only. Entry observed at "
            f"{policy.entry_time.strftime('%H:%M')} ET, settlement at "
            f"{policy.exit_time.strftime('%H:%M')} ET on the first post-earnings trading day."
        ),
    )


def get_system_health(  # noqa: PLR0912, PLR0915 -- one aggregation, kept in one place
    db: Session,
    settings: Settings,
    scheduler_status: SchedulerStatus,
    tws_health_probe: TwsHealthProbe | None = None,
    tws_provider: IBKRTWSProvider | None = None,
) -> SystemHealth:
    ibkr_run = _latest_scheduler_run(db, IBKR_GATEWAY_HEALTHCHECK_JOB_ID)
    provider = settings.ibkr_provider.lower()
    if provider == "tws":
        tws_status = get_tws_status(settings, probe=tws_health_probe, provider=tws_provider)
        if tws_status.status_label == "CONNECTED":
            ibkr_state = _HEALTHY
        elif tws_status.status_label == "AUTH_REQUIRED":
            ibkr_state = _DEGRADED
        else:
            ibkr_state = _FAILED
        ibkr = IbkrHealth(
            state=ibkr_state,
            gateway_reachable=tws_status.gateway_reachable,
            authenticated=tws_status.api_ready,
            connected=tws_status.socket_connected,
            live_account=None,
            market_data_quality=tws_status.market_data_quality,
            last_heartbeat_at=ibkr_run.started_at if ibkr_run else None,
            last_error=tws_status.error,
            provider="tws",
        )
    else:
        ibkr_status = get_ibkr_status(settings)
        live_account = _is_live_ibkr_account(settings, ibkr_status)
        if ibkr_status.status_label == "CONNECTED":
            ibkr_state = _HEALTHY
        elif ibkr_status.status_label == "AUTH_REQUIRED":
            ibkr_state = _DEGRADED
        else:
            ibkr_state = _FAILED
        ibkr = IbkrHealth(
            state=ibkr_state,
            gateway_reachable=ibkr_status.gateway_reachable,
            authenticated=ibkr_status.authenticated,
            connected=ibkr_status.connected,
            live_account=live_account,
            market_data_quality=None,
            last_heartbeat_at=ibkr_run.started_at if ibkr_run else None,
            last_error=ibkr_status.error,
            provider="web",
        )

    domains = get_provider_dashboard(db, settings)
    calendar_domain = next((d for d in domains if d.domain == "earnings_calendar"), None)
    calendar_sync_run = _latest_scheduler_run(db, CALENDAR_SYNC_JOB_ID)
    calendar_error = None
    calendar_state = _NOT_APPLICABLE
    if calendar_domain is not None:
        active = next(
            (p for p in calendar_domain.providers if p.provider == calendar_domain.primary), None
        )
        if active is not None and active.configured:
            calendar_state = _HEALTHY
            if calendar_sync_run is not None and calendar_sync_run.status == "error":
                calendar_state = _FAILED
                calendar_error = calendar_sync_run.error_summary
            elif active.last_success_at is None:
                calendar_state = _DEGRADED
        earnings_calendar = EarningsCalendarHealth(
            state=calendar_state,
            active_provider=calendar_domain.primary,
            fallback_provider=calendar_domain.fallback,
            last_successful_sync_at=active.last_success_at if active else None,
            events_received=calendar_sync_run.items_evaluated if calendar_sync_run else None,
            last_error=calendar_error,
            next_scheduled_sync_at=next(
                (
                    j.next_run_time
                    for j in scheduler_status.jobs
                    if j.job_id == CALENDAR_SYNC_JOB_ID
                ),
                None,
            ),
        )
    else:
        earnings_calendar = EarningsCalendarHealth(
            _NOT_APPLICABLE, None, None, None, None, None, None
        )

    llm_domain = next((d for d in domains if d.domain == "llm"), None)
    ai_provider_name = llm_domain.primary if llm_domain else settings.llm_provider
    ai_active = None
    if llm_domain is not None:
        ai_active = next(
            (p for p in llm_domain.providers if p.provider == llm_domain.primary), None
        )
    ai_error_event = _last_provider_error(db, ai_provider_name, "llm") if ai_provider_name else None
    last_view_generated_at = db.query(func.max(V4ShadowDecision.generated_at)).scalar()
    decision_run = _latest_scheduler_run(db, V4_SHADOW_DECISION_JOB_ID)
    decision_run_error_summary = (
        decision_run.error_summary
        if decision_run is not None and decision_run.status == "error"
        else None
    )
    from services.v4_decision_view_config import (  # noqa: PLC0415
        describe_v4_decision_view_config,
    )

    dv = describe_v4_decision_view_config(settings)
    ai_state = _NOT_APPLICABLE
    if ai_active is not None:
        ai_state = _HEALTHY if ai_active.configured else _DEGRADED
        if ai_error_event is not None or decision_run_error_summary is not None:
            ai_state = _DEGRADED
        if dv.get("config_error"):
            ai_state = _DEGRADED
    ai_provider = AiProviderHealth(
        state=ai_state,
        provider=ai_provider_name or "unknown",
        configured=bool(ai_active and ai_active.configured),
        last_successful_generation_at=last_view_generated_at,
        last_error=decision_run_error_summary
        or (ai_error_event.detail if ai_error_event else None),
        decision_view_model=dv.get("model"),
        decision_view_thinking=dv.get("thinking"),
        decision_view_reasoning_effort=dv.get("reasoning_effort"),
        decision_view_max_tokens=dv.get("max_tokens"),
        decision_view_config_error=dv.get("config_error"),
    )

    scheduler = SchedulerHealth(
        state=_HEALTHY if scheduler_status.running else _FAILED,
        running=scheduler_status.running,
        registered_job_count=len(scheduler_status.jobs),
        # Judged from persisted runs (any job), never from the in-process job
        # objects, which do not remember when they last fired.
        last_activity_at=_latest_successful_run_started_at(db)
        or max(
            (j.last_run_at for j in scheduler_status.jobs if j.last_run_at is not None),
            default=None,
        ),
        next_activity_at=min(
            (j.next_run_time for j in scheduler_status.jobs if j.next_run_time is not None),
            default=None,
        ),
    )
    return SystemHealth(
        ibkr=ibkr,
        earnings_calendar=earnings_calendar,
        ai_provider=ai_provider,
        scheduler=scheduler,
        database=_get_database_health(db),
        v4_shadow=get_v4_shadow_health(db, settings),
    )


# ---------------------------------------------------------------------------
# Research readiness + the V4 pipeline
# ---------------------------------------------------------------------------


def _passes_business_filters(event: EarningsCalendarEvent) -> tuple[bool, str | None]:
    if event.market_cap is None:
        return False, "market cap unknown"
    try:
        cap = Decimal(str(event.market_cap))
    except (InvalidOperation, ValueError):
        return False, "market cap unreadable"
    if cap < MIN_MARKET_CAP:
        return False, f"market cap below ${MIN_MARKET_CAP:,.0f}"
    if event.country is None:
        return False, "listing country unknown"
    if event.country.upper() != US_COUNTRY_CODE:
        return False, f"not US listed (country={event.country})"
    return True, None


def _v4_schedule(event: EarningsCalendarEvent) -> EarningsEntryExitSchedule:
    return compute_entry_exit_schedule(
        event.earnings_date, announcement_session(event), policy=V4_ACTIVE_TIMING_POLICY
    )


@dataclass(frozen=True)
class TimelineStep:
    label: str
    at: datetime | None
    status: str  # done | pending | failed | warning | skipped
    detail: str | None = None


@dataclass(frozen=True)
class V4PipelineEvent:
    calendar_event_id: int
    symbol: str
    company_name: str
    market_cap: str | None
    earnings_date: str
    earnings_timing: str
    entry_timestamp: datetime
    exit_timestamp: datetime
    lifecycle_state: str
    lifecycle_reason: str | None
    next_action: str | None
    next_action_at: datetime | None
    research_ready: bool
    shadow_decision_id: int | None
    decision_status: str | None
    entries_observed: int
    entries_failed: int
    settlements_settled: int
    settlements_failed: int
    timeline: list[TimelineStep]


_THESIS_FRESH = timedelta(days=THESIS_FRESHNESS_DAYS)
_RESOLUTION_MARKER = "supported symbol"


def _latest_prep_job(db: Session, symbol: str) -> ResearchPreparationJob | None:
    return (
        db.query(ResearchPreparationJob)
        .filter(ResearchPreparationJob.ticker == symbol)
        .order_by(ResearchPreparationJob.id.desc())
        .first()
    )


def _latest_thesis(db: Session, company_id: int) -> AIThesisVersion | None:
    return (
        db.query(AIThesisVersion)
        .filter_by(company_id=company_id)
        .order_by(AIThesisVersion.created_at.desc())
        .first()
    )


@dataclass
class _Facts:
    event: EarningsCalendarEvent
    schedule: EarningsEntryExitSchedule
    now: datetime
    company: Company | None
    prep: ResearchPreparationJob | None
    thesis: AIThesisVersion | None
    thesis_fresh: bool
    decision: V4ShadowDecision | None
    run_events: list[V4ShadowRunEvent]
    entries: list[V4ShadowConfigEntry]
    settlements: list[V4ShadowConfigSettlement]
    timeline: list[TimelineStep]

    @property
    def research_ready(self) -> bool:
        return self.company is not None and self.thesis_fresh

    def row(  # noqa: PLR0913
        self,
        state: str,
        reason: str | None = None,
        next_action: str | None = None,
        next_at: datetime | None = None,
    ) -> V4PipelineEvent:
        e = self.event
        return V4PipelineEvent(
            calendar_event_id=e.id,
            symbol=e.symbol,
            company_name=e.company_name,
            market_cap=str(e.market_cap) if e.market_cap is not None else None,
            earnings_date=e.earnings_date.isoformat(),
            earnings_timing=str(getattr(e.earnings_time, "value", e.earnings_time)),
            entry_timestamp=self.schedule.entry_timestamp,
            exit_timestamp=self.schedule.exit_timestamp,
            lifecycle_state=state,
            lifecycle_reason=reason,
            next_action=next_action,
            next_action_at=next_at,
            research_ready=self.research_ready,
            shadow_decision_id=self.decision.id if self.decision else None,
            decision_status=self.decision.status if self.decision else None,
            entries_observed=sum(1 for x in self.entries if x.status == "OBSERVED"),
            entries_failed=sum(1 for x in self.entries if x.status != "OBSERVED"),
            settlements_settled=sum(1 for s in self.settlements if s.status == "SETTLED"),
            settlements_failed=sum(1 for s in self.settlements if s.status != "SETTLED"),
            timeline=list(self.timeline),
        )


def _gather(db: Session, event: EarningsCalendarEvent, now: datetime) -> _Facts:
    company = db.query(Company).filter_by(ticker=event.symbol).one_or_none()
    thesis = _latest_thesis(db, company.id) if company else None
    decision = (
        db.query(V4ShadowDecision)
        .filter_by(earnings_calendar_event_id=event.id)
        .order_by(V4ShadowDecision.id.desc())
        .first()
    )
    entries: list[V4ShadowConfigEntry] = []
    settlements: list[V4ShadowConfigSettlement] = []
    if decision is not None:
        entries = db.query(V4ShadowConfigEntry).filter_by(shadow_decision_id=decision.id).all()
        settlements = (
            db.query(V4ShadowConfigSettlement).filter_by(shadow_decision_id=decision.id).all()
        )
    return _Facts(
        event=event,
        schedule=_v4_schedule(event),
        now=now,
        company=company,
        prep=_latest_prep_job(db, event.symbol),
        thesis=thesis,
        thesis_fresh=thesis is not None and (now - thesis.created_at) < _THESIS_FRESH,
        decision=decision,
        run_events=(
            db.query(V4ShadowRunEvent)
            .filter_by(earnings_calendar_event_id=event.id)
            .order_by(V4ShadowRunEvent.occurred_at.desc())
            .all()
        ),
        entries=entries,
        settlements=settlements,
        timeline=[
            TimelineStep(
                "Calendar event discovered",
                event.created_at,
                "done",
                f"source: {getattr(event.source, 'value', event.source)}",
            )
        ],
    )


def _research_timeline(f: _Facts) -> None:
    prep, status = f.prep, (f.prep.status if f.prep else None)
    if f.company is None:
        if prep is not None and status == JobStatus.FAILED:
            resolution = _RESOLUTION_MARKER in (prep.error or "")
            f.timeline.append(
                TimelineStep(
                    "Company resolution" if resolution else "Research preparation",
                    prep.completed_at,
                    "failed",
                    prep.error,
                )
            )
        elif prep is not None and status in (JobStatus.PENDING, JobStatus.INTERRUPTED):
            f.timeline.append(
                TimelineStep("Research preparation", prep.started_at, "pending", "queued")
            )
        elif prep is not None and status == JobStatus.RUNNING:
            f.timeline.append(
                TimelineStep("Research preparation", prep.started_at, "pending", "running")
            )
        else:
            f.timeline.append(
                TimelineStep("Research preparation", None, "pending", "not yet queued")
            )
        return
    if f.thesis_fresh and f.thesis is not None:
        f.timeline.append(
            TimelineStep(
                "Research + AI thesis", f.thesis.created_at, "done", f"thesis id={f.thesis.id}"
            )
        )
    elif status in (JobStatus.PENDING, JobStatus.INTERRUPTED, JobStatus.RUNNING):
        f.timeline.append(
            TimelineStep(
                "Research + AI thesis",
                prep.started_at if prep else None,
                "pending",
                "preparation in progress",
            )
        )
    else:
        detail = (
            "no AI thesis"
            if f.thesis is None
            else f"thesis is {(f.now - f.thesis.created_at).days}d old"
        )
        f.timeline.append(TimelineStep("Research + AI thesis", None, "warning", detail))


def _decided_row(f: _Facts) -> V4PipelineEvent:
    d = f.decision
    assert d is not None
    f.timeline.append(
        TimelineStep(
            "V4 decision", d.generated_at, "failed" if d.status == "FAILED" else "done", d.status
        )
    )
    if d.status == "NO_ACTION":
        f.timeline.append(TimelineStep("Entry", None, "skipped", "no action"))
        return f.row(STATE_NO_ACTION, d.no_action_reason)
    if d.status == "FAILED":
        return f.row(STATE_DECISION_FAILED, d.no_action_reason)
    observed = [e for e in f.entries if e.status == "OBSERVED"]
    if not observed:
        detail = f.entries[0].failure_detail if f.entries else "no configuration entry rows"
        f.timeline.append(
            TimelineStep(
                "Entry observation",
                f.entries[0].observed_at if f.entries else None,
                "failed",
                detail,
            )
        )
        return f.row(STATE_ENTRY_FAILED, detail)
    f.timeline.append(
        TimelineStep(
            "Entry observation",
            observed[0].observed_at,
            "done",
            f"{len(observed)}/{len(f.entries)} configurations observed",
        )
    )
    if f.settlements:
        failed = [s for s in f.settlements if s.status != "SETTLED"]
        if failed:
            f.timeline.append(
                TimelineStep("Settlement", failed[0].settled_at, "failed", failed[0].failure_detail)
            )
            return f.row(STATE_SETTLEMENT_FAILED, failed[0].failure_detail)
        f.timeline.append(
            TimelineStep(
                "Settlement",
                f.settlements[0].settled_at,
                "done",
                f"{len(f.settlements)} configurations settled",
            )
        )
        return f.row(STATE_SETTLED)
    f.timeline.append(
        TimelineStep(
            "Settlement",
            f.schedule.exit_timestamp,
            "pending",
            f"scheduled {V4_ACTIVE_TIMING_POLICY.exit_time.strftime('%H:%M')} ET",
        )
    )
    return f.row(STATE_WAITING_SETTLEMENT, None, "Observe settlement", f.schedule.exit_timestamp)


def _window_passed_row(f: _Facts) -> V4PipelineEvent:
    latest = f.run_events[0] if f.run_events else None
    if latest is not None:
        f.timeline.append(TimelineStep("V4 decision", latest.occurred_at, "failed", latest.message))
        if latest.category == "DEADLINE_SKIPPED":
            return f.row(STATE_DEADLINE_SKIPPED, latest.message)
        if latest.category == "RESEARCH_NOT_READY":
            return f.row(STATE_RESEARCH_NOT_READY, latest.message)
        return f.row(STATE_DECISION_FAILED, f"{latest.category}: {latest.message}")
    f.timeline.append(
        TimelineStep(
            "V4 decision", f.schedule.entry_timestamp, "failed", "no run recorded for this window"
        )
    )
    return f.row(STATE_DECISION_WINDOW_MISSED, "the decision window passed with no recorded run")


def _pending_row(f: _Facts) -> V4PipelineEvent:
    prep, status = f.prep, (f.prep.status if f.prep else None)
    if f.company is None:
        if prep is not None and status == JobStatus.FAILED:
            if _RESOLUTION_MARKER in (prep.error or ""):
                return f.row(STATE_COMPANY_RESOLUTION_FAILED, prep.error)
            return f.row(STATE_RESEARCH_FAILED, prep.error, "Retry research preparation")
        if status in (JobStatus.PENDING, JobStatus.INTERRUPTED):
            return f.row(
                STATE_RESEARCH_QUEUED, "queued for the research worker", "Research preparation"
            )
        if status == JobStatus.RUNNING:
            return f.row(
                STATE_RESEARCH_RUNNING,
                "the research worker is preparing this company",
                "Research preparation",
            )
        return f.row(
            STATE_CALENDAR_DISCOVERED,
            "no Company row yet -- queued by the next preparation pass",
            "Research preparation",
        )
    if not f.thesis_fresh:
        if status in (JobStatus.PENDING, JobStatus.INTERRUPTED):
            return f.row(
                STATE_RESEARCH_QUEUED, "queued: AI thesis missing or stale", "Research preparation"
            )
        if status == JobStatus.RUNNING:
            return f.row(
                STATE_RESEARCH_RUNNING,
                "preparing: AI thesis missing or stale",
                "Research preparation",
            )
        if status == JobStatus.FAILED:
            return f.row(
                STATE_RESEARCH_FAILED, prep.error if prep else None, "Retry research preparation"
            )
        return f.row(
            STATE_CALENDAR_DISCOVERED,
            "not V4-ready: no fresh AI thesis -- queued by the next preparation pass",
            "Research preparation",
        )
    f.timeline.append(
        TimelineStep(
            "V4 decision",
            f.schedule.entry_timestamp,
            "pending",
            f"scheduled {V4_ACTIVE_TIMING_POLICY.entry_time.strftime('%H:%M')} ET",
        )
    )
    action = "V4 decision + entry observation"
    if f.schedule.entry_timestamp - f.now <= timedelta(hours=24):
        return f.row(STATE_WAITING_DECISION, None, action, f.schedule.entry_timestamp)
    return f.row(STATE_RESEARCH_READY, None, action, f.schedule.entry_timestamp)


def classify_event(db: Session, event: EarningsCalendarEvent, now: datetime) -> V4PipelineEvent:
    """The V4 lifecycle of one calendar event, derived only from persisted rows."""
    f = _gather(db, event, now)
    eligible, why_not = _passes_business_filters(event)
    if not eligible:
        f.timeline.append(TimelineStep("Business eligibility", None, "failed", why_not))
        return f.row(STATE_BUSINESS_INELIGIBLE, why_not)
    f.timeline.append(TimelineStep("Business eligibility", None, "done", None))
    _research_timeline(f)
    if f.decision is not None:
        return _decided_row(f)
    if now > f.schedule.entry_timestamp + LATE_CUTOFF_GRACE:
        return _window_passed_row(f)
    return _pending_row(f)


def get_v4_pipeline(db: Session, *, now: datetime | None = None) -> list[V4PipelineEvent]:
    """One row per real calendar event whose earnings date falls inside
    [today - 2 days, today + 7 days] (Eastern), with the V4 lifecycle state."""
    now = now or datetime.now(UTC)
    today = now.astimezone(EASTERN).date()
    events = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.earnings_date >= today - timedelta(days=PIPELINE_LOOKBACK_DAYS),
            EarningsCalendarEvent.earnings_date <= today + timedelta(days=PIPELINE_WINDOW_DAYS),
        )
        .order_by(
            EarningsCalendarEvent.earnings_date,
            EarningsCalendarEvent.market_cap.desc().nullslast(),
        )
        .all()
    )
    rows = []
    for event in events:
        try:
            rows.append(classify_event(db, event, now))
        except Exception:  # noqa: BLE001 -- one event must never blank the monitor
            log.exception("operations: could not classify event %s", event.id)
    return rows


@dataclass(frozen=True)
class ResearchReadiness:
    """KPIs over the upcoming window (events whose V4 decision window has not
    closed yet, up to PIPELINE_WINDOW_DAYS ahead)."""

    window_days: int
    upcoming_events: int
    business_eligible: int
    company_resolved: int
    research_queued: int
    research_running: int
    research_ready: int
    research_failed: int
    ai_thesis_ready: int
    v4_decision_ready: int
    next_window_at: datetime | None
    next_window_ready: int
    next_window_total: int


def compute_research_readiness(
    pipeline: list[V4PipelineEvent], *, now: datetime | None = None
) -> ResearchReadiness:
    now = now or datetime.now(UTC)
    upcoming = [p for p in pipeline if p.entry_timestamp + LATE_CUTOFF_GRACE >= now]
    eligible = [p for p in upcoming if p.lifecycle_state != STATE_BUSINESS_INELIGIBLE]
    queued = sum(1 for p in eligible if p.lifecycle_state == STATE_RESEARCH_QUEUED)
    running = sum(1 for p in eligible if p.lifecycle_state == STATE_RESEARCH_RUNNING)
    failed = sum(
        1
        for p in eligible
        if p.lifecycle_state in (STATE_RESEARCH_FAILED, STATE_COMPANY_RESOLUTION_FAILED)
    )
    ready = sum(1 for p in eligible if p.research_ready)
    resolved = sum(
        1
        for p in eligible
        if p.research_ready or "no Company row" not in (p.lifecycle_reason or "")
    )
    next_window_at = min((p.entry_timestamp for p in eligible), default=None)
    next_window = (
        [p for p in eligible if p.entry_timestamp == next_window_at] if next_window_at else []
    )
    return ResearchReadiness(
        window_days=PIPELINE_WINDOW_DAYS,
        upcoming_events=len(upcoming),
        business_eligible=len(eligible),
        company_resolved=resolved,
        research_queued=queued,
        research_running=running,
        research_ready=ready,
        research_failed=failed,
        ai_thesis_ready=ready,
        v4_decision_ready=ready,
        next_window_at=next_window_at,
        next_window_ready=sum(1 for p in next_window if p.research_ready),
        next_window_total=len(next_window),
    )


# ---------------------------------------------------------------------------
# Scheduler job monitor
# ---------------------------------------------------------------------------

ALL_JOB_IDS = (
    CALENDAR_SYNC_JOB_ID,
    EARNINGS_RESEARCH_PREPARATION_JOB_ID,
    RESEARCH_READINESS_CATCHUP_JOB_ID,
    IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
    V4_FORWARD_WINDOW_JOB_ID,
    V4_SHADOW_DECISION_JOB_ID,
    V4_SHADOW_SETTLEMENT_JOB_ID,
)

#: Phases of the forward-window coordinator: they record their own runs but
#: are never registered separately, so their schedule is the coordinator's.
FORWARD_WINDOW_PHASE_JOB_IDS = (V4_SHADOW_SETTLEMENT_JOB_ID, V4_SHADOW_DECISION_JOB_ID)


@dataclass(frozen=True)
class SchedulerJobView:
    job_id: str
    enabled: bool
    last_run_at: datetime | None
    last_run_status: str | None
    duration_ms: int | None
    items_evaluated: int | None
    items_succeeded: int | None
    items_failed: int | None
    next_run_time: datetime | None
    last_error: str | None


def get_scheduler_jobs(db: Session, scheduler_status: SchedulerStatus) -> list[SchedulerJobView]:
    """The fixed platform/V4 set is always listed (a missing job is a
    visible enabled=False row); any further registered job is appended in
    registration order."""
    status_by_job_id = {j.job_id: j for j in scheduler_status.jobs}
    job_ids = list(ALL_JOB_IDS) + [
        j.job_id for j in scheduler_status.jobs if j.job_id not in ALL_JOB_IDS
    ]
    views: list[SchedulerJobView] = []
    for job_id in job_ids:
        status = status_by_job_id.get(job_id)
        if status is None and job_id in FORWARD_WINDOW_PHASE_JOB_IDS:
            # A phase is scheduled exactly when its coordinator is.
            status = status_by_job_id.get(V4_FORWARD_WINDOW_JOB_ID)
        latest_run = _latest_scheduler_run(db, job_id)
        views.append(
            SchedulerJobView(
                job_id=job_id,
                enabled=status is not None,
                last_run_at=(
                    latest_run.started_at
                    if latest_run
                    else (status.last_run_at if status else None)
                ),
                last_run_status=(
                    latest_run.status
                    if latest_run
                    else (status.last_run_status if status else None)
                ),
                duration_ms=latest_run.duration_ms if latest_run else None,
                items_evaluated=latest_run.items_evaluated if latest_run else None,
                items_succeeded=latest_run.items_succeeded if latest_run else None,
                items_failed=latest_run.items_failed if latest_run else None,
                next_run_time=status.next_run_time if status else None,
                last_error=(
                    latest_run.error_summary
                    if latest_run is not None and latest_run.status == "error"
                    else None
                ),
            )
        )
    return views


# ---------------------------------------------------------------------------
# Research preparation progress (durable queue)
# ---------------------------------------------------------------------------

_QUEUE_MANAGED = ResearchPreparationJob.earnings_calendar_event_id.isnot(None)
_STEP_ORDER = list(PreparationStep)


@dataclass(frozen=True)
class PreparationProgress:
    queue_depth: int
    completed: int
    failed: int
    worker_active: bool
    current_symbol: str | None
    current_stage: str | None
    step_index: int | None
    step_total: int | None
    attempt: int | None
    heartbeat_seconds_ago: float | None
    elapsed_seconds: float | None


def get_preparation_progress(db: Session, *, now: datetime | None = None) -> PreparationProgress:
    now = now or datetime.now(UTC)
    queue_depth = count_queue_depth(db)
    completed = (
        db.query(func.count(ResearchPreparationJob.id))
        .filter(
            _QUEUE_MANAGED,
            ResearchPreparationJob.status.in_(
                (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS)
            ),
        )
        .scalar()
        or 0
    )
    failed = (
        db.query(func.count(ResearchPreparationJob.id))
        .filter(_QUEUE_MANAGED, ResearchPreparationJob.status == JobStatus.FAILED)
        .scalar()
        or 0
    )
    current_job = (
        db.query(ResearchPreparationJob)
        .filter(_QUEUE_MANAGED, ResearchPreparationJob.status == JobStatus.RUNNING)
        .order_by(ResearchPreparationJob.heartbeat_at.desc().nullslast())
        .first()
    )
    if current_job is None:
        return PreparationProgress(
            queue_depth, completed, failed, False, None, None, None, None, None, None, None
        )
    current_stage = None
    step_index = None
    running_step = next((s for s in current_job.steps if s.get("status") == "running"), None)
    if running_step is not None:
        step = PreparationStep(running_step["step"])
        current_stage = STEP_LABELS.get(step, step.value)
        step_index = _STEP_ORDER.index(step) + 1
    return PreparationProgress(
        queue_depth=queue_depth,
        completed=completed,
        failed=failed,
        worker_active=True,
        current_symbol=current_job.ticker,
        current_stage=current_stage,
        step_index=step_index,
        step_total=len(_STEP_ORDER),
        attempt=current_job.attempt_count,
        heartbeat_seconds_ago=(
            (now - current_job.heartbeat_at).total_seconds()
            if current_job.heartbeat_at is not None
            else None
        ),
        elapsed_seconds=(
            (now - current_job.started_at).total_seconds()
            if current_job.started_at is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Failure centre + missed / stale run alerts
# ---------------------------------------------------------------------------

FAILURE_LOOKBACK = timedelta(days=3)
RESEARCH_PREPARATION_STALE_AFTER = timedelta(hours=26)
CALENDAR_SYNC_STALE_AFTER = timedelta(hours=30)
MISSED_JOB_GRACE = timedelta(minutes=5)

_V4_FAILURE_CATEGORIES_RETRYABLE = {"INTERNAL_ERROR", "VIEW_GENERATION_FAILED"}
_V4_NON_FAILURE_CATEGORIES = {"OK"}


@dataclass(frozen=True)
class FailureEntry:
    occurred_at: datetime
    symbol: str | None
    stage: str
    category: str
    explanation: str
    detail: str | None
    retryability: str


def _v4_run_event_failures(db: Session, since: datetime) -> list[FailureEntry]:
    failures: list[FailureEntry] = []
    events = (
        db.query(V4ShadowRunEvent)
        .filter(V4ShadowRunEvent.occurred_at >= since)
        .order_by(V4ShadowRunEvent.occurred_at.desc())
        .all()
    )
    not_ready_by_day: dict[date, list[V4ShadowRunEvent]] = {}
    for e in events:
        if e.category == "RESEARCH_NOT_READY":
            not_ready_by_day.setdefault(e.occurred_at.astimezone(EASTERN).date(), []).append(e)
            continue
        if e.category in _V4_NON_FAILURE_CATEGORIES:
            continue
        if e.category in ("DEADLINE_SKIPPED", "SETTLEMENT_WINDOW_MISSED"):
            retry = WINDOW_MISSED
        elif e.retryable or e.category in _V4_FAILURE_CATEGORIES_RETRYABLE:
            retry = RETRYABLE
        else:
            retry = NOT_RETRYABLE
        failures.append(
            FailureEntry(
                occurred_at=e.occurred_at,
                symbol=e.ticker,
                stage=e.stage,
                category=e.category,
                explanation=f"{e.category.replace('_', ' ').capitalize()} for {e.ticker}",
                detail=e.message,
                retryability=retry,
            )
        )
    for day, group in not_ready_by_day.items():
        symbols = sorted({e.ticker for e in group if e.ticker})
        failures.append(
            FailureEntry(
                occurred_at=max(e.occurred_at for e in group),
                symbol=None,
                stage="research_gate",
                category="RESEARCH_NOT_READY",
                explanation=(
                    f"{len(symbols)} event(s) were not research-ready at the {day.isoformat()} "
                    "decision window"
                ),
                detail=", ".join(symbols[:40]) + (" …" if len(symbols) > 40 else ""),
                retryability=WINDOW_MISSED,
            )
        )
    return failures


def get_recent_failures(db: Session, *, now: datetime | None = None) -> list[FailureEntry]:
    now = now or datetime.now(UTC)
    since = now - FAILURE_LOOKBACK
    failures = _v4_run_event_failures(db, since)
    for run in (
        db.query(SchedulerRun)
        .filter(SchedulerRun.status == "error", SchedulerRun.started_at >= since)
        .order_by(SchedulerRun.started_at.desc())
        .all()
    ):
        failures.append(
            FailureEntry(
                occurred_at=run.started_at,
                symbol=None,
                stage=run.job_id,
                category="scheduler_run_error",
                explanation=f"Scheduler job {run.job_id} failed to complete",
                detail=run.error_summary,
                retryability=RETRYABLE,
            )
        )
    for job in (
        db.query(ResearchPreparationJob)
        .filter(
            ResearchPreparationJob.status == JobStatus.FAILED,
            ResearchPreparationJob.completed_at >= since,
        )
        .order_by(ResearchPreparationJob.completed_at.desc())
        .all()
    ):
        resolution = _RESOLUTION_MARKER in (job.error or "")
        failures.append(
            FailureEntry(
                occurred_at=job.completed_at or job.started_at,
                symbol=job.ticker,
                stage="research_preparation",
                category="COMPANY_RESOLUTION_FAILED" if resolution else "RESEARCH_FAILED",
                explanation=(
                    f"Company could not be resolved for {job.ticker}"
                    if resolution
                    else f"Research preparation failed for {job.ticker}"
                ),
                detail=job.error,
                retryability=NOT_RETRYABLE if resolution else RETRYABLE,
            )
        )
    for ev in (
        db.query(ProviderHealthEvent)
        .filter(ProviderHealthEvent.occurred_at >= since)
        .order_by(ProviderHealthEvent.occurred_at.desc())
        .all()
    ):
        status_value = str(getattr(ev.status, "value", ev.status))
        if status_value.lower() == "connected":
            continue
        failures.append(
            FailureEntry(
                occurred_at=ev.occurred_at,
                symbol=None,
                stage=ev.domain,
                category=f"provider_{status_value.lower()}",
                explanation=f"{ev.provider} ({ev.domain}) reported {status_value}",
                detail=ev.detail,
                retryability=RETRYABLE,
            )
        )
    failures.sort(key=lambda f: f.occurred_at, reverse=True)
    return failures


@dataclass(frozen=True)
class JobStaleness:
    job_id: str
    state: str  # ok | stale | missed | never
    last_expected_at: datetime | None
    last_actual_at: datetime | None
    next_run_at: datetime | None
    detail: str


def detect_missed_job_alerts(
    db: Session,
    scheduler_jobs: list[SchedulerJobView],
    pipeline: list[V4PipelineEvent],
    *,
    now: datetime | None = None,
) -> tuple[list[FailureEntry], list[JobStaleness]]:
    """Stale / missed scheduled work, judged from persisted runs -- never
    from the job merely being registered."""
    now = now or datetime.now(UTC)
    alerts: list[FailureEntry] = []
    staleness: list[JobStaleness] = []
    by_id = {j.job_id: j for j in scheduler_jobs}

    def _stale_check(
        job_id: str, stale_after: timedelta, label: str, *, also: tuple[str, ...] = ()
    ) -> None:
        # Freshness is judged from the most recent success of the job OR of
        # any job that does the same work (the research catch-up passes).
        candidates = [_latest_successful_scheduler_run(db, j) for j in (job_id, *also)]
        found = [r for r in candidates if r is not None]
        last_ok = max(found, key=lambda r: r.started_at) if found else None
        job = by_id.get(job_id)
        next_at = job.next_run_time if job else None
        expected = next_at - timedelta(days=1) if next_at else None
        if last_ok is None:
            detail = f"{label} has never completed successfully"
            staleness.append(JobStaleness(job_id, "never", expected, None, next_at, detail))
            alerts.append(FailureEntry(now, None, job_id, "job_never_ran", detail, None, RETRYABLE))
            return
        age_h = (now - last_ok.started_at).total_seconds() / 3600
        if now - last_ok.started_at > stale_after:
            detail = f"last successful run {age_h:.1f}h ago (expected daily)"
            staleness.append(
                JobStaleness(job_id, "stale", expected, last_ok.started_at, next_at, detail)
            )
            alerts.append(
                FailureEntry(
                    now, None, job_id, "job_stale", f"{label} is STALE: {detail}", None, RETRYABLE
                )
            )
        else:
            staleness.append(
                JobStaleness(
                    job_id,
                    "ok",
                    expected,
                    last_ok.started_at,
                    next_at,
                    f"last run {age_h:.1f}h ago",
                )
            )

    _stale_check(
        EARNINGS_RESEARCH_PREPARATION_JOB_ID,
        RESEARCH_PREPARATION_STALE_AFTER,
        "Research preparation",
        also=(RESEARCH_READINESS_CATCHUP_JOB_ID, RESEARCH_PREPARATION_STARTUP_CATCHUP_JOB_ID),
    )
    _stale_check(CALENDAR_SYNC_JOB_ID, CALENDAR_SYNC_STALE_AFTER, "Calendar sync")

    today = now.astimezone(EASTERN).date()
    due_today = [p for p in pipeline if p.entry_timestamp.astimezone(EASTERN).date() == today]
    if due_today:
        window = due_today[0].entry_timestamp
        if now > window + LATE_CUTOFF_GRACE + MISSED_JOB_GRACE:
            run = _latest_scheduler_run(db, V4_SHADOW_DECISION_JOB_ID)
            ran_today = run is not None and run.started_at.astimezone(EASTERN).date() == today
            if not ran_today:
                detail = (
                    f"V4 decision job did not run for today's "
                    f"{window.astimezone(EASTERN).strftime('%H:%M')} ET window "
                    f"({len(due_today)} due event(s))"
                )
                alerts.append(
                    FailureEntry(
                        now,
                        None,
                        V4_SHADOW_DECISION_JOB_ID,
                        "job_missed",
                        detail,
                        None,
                        WINDOW_MISSED,
                    )
                )
                job = by_id.get(V4_SHADOW_DECISION_JOB_ID)
                staleness.append(
                    JobStaleness(
                        V4_SHADOW_DECISION_JOB_ID,
                        "missed",
                        window,
                        run.started_at if run else None,
                        job.next_run_time if job else None,
                        "no run recorded for today's window",
                    )
                )
    settle_today = [
        p
        for p in pipeline
        if p.lifecycle_state == STATE_WAITING_SETTLEMENT
        and p.exit_timestamp.astimezone(EASTERN).date() == today
    ]
    if settle_today:
        window = settle_today[0].exit_timestamp
        if now > window + LATE_CUTOFF_GRACE + MISSED_JOB_GRACE:
            run = _latest_scheduler_run(db, V4_SHADOW_SETTLEMENT_JOB_ID)
            ran_today = run is not None and run.started_at.astimezone(EASTERN).date() == today
            if not ran_today:
                detail = (
                    f"V4 settlement job did not run for today's "
                    f"{window.astimezone(EASTERN).strftime('%H:%M')} ET window "
                    f"({len(settle_today)} position(s) due)"
                )
                alerts.append(
                    FailureEntry(
                        now,
                        None,
                        V4_SHADOW_SETTLEMENT_JOB_ID,
                        "job_missed",
                        detail,
                        None,
                        WINDOW_MISSED,
                    )
                )
    return alerts, staleness


# ---------------------------------------------------------------------------
# Pre-flight readiness for today's 15:30 ET window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightCheck:
    label: str
    passed: bool
    detail: str | None = None


@dataclass(frozen=True)
class PreflightReadiness:
    checks: list[PreflightCheck]
    ready: bool
    blockers: list[str]


def get_preflight_readiness(
    db: Session,
    health: SystemHealth,
    readiness: ResearchReadiness,
    staleness: list[JobStaleness],
    *,
    now: datetime | None = None,
) -> PreflightReadiness:
    del db, now  # signature kept uniform with the other read models
    checks = [
        PreflightCheck("TWS market data connected", health.ibkr.connected, health.ibkr.last_error),
        PreflightCheck(
            "V4 DecisionView model configured",
            health.ai_provider.configured and not health.ai_provider.decision_view_config_error,
            health.ai_provider.decision_view_config_error
            or (
                f"{health.ai_provider.decision_view_model} · thinking "
                f"{health.ai_provider.decision_view_thinking}"
            ),
        ),
        PreflightCheck("Scheduler running", health.scheduler.running, None),
        PreflightCheck(
            "V4 forward test enabled",
            bool(health.v4_shadow and health.v4_shadow.enabled),
            health.v4_shadow.note if health.v4_shadow else None,
        ),
    ]
    for js in staleness:
        checks.append(PreflightCheck(f"{js.job_id} on schedule", js.state == "ok", js.detail))
    checks.append(
        PreflightCheck(
            "Next V4 window has research-ready events",
            readiness.next_window_total == 0 or readiness.next_window_ready > 0,
            (
                f"{readiness.next_window_ready}/{readiness.next_window_total} ready for "
                f"{readiness.next_window_at.astimezone(EASTERN).strftime('%Y-%m-%d %H:%M')} ET"
                if readiness.next_window_at
                else "no eligible events in the window"
            ),
        )
    )
    blockers = [c.label for c in checks if not c.passed]
    return PreflightReadiness(checks=checks, ready=not blockers, blockers=blockers)


# ---------------------------------------------------------------------------
# Today's V4 summary + market clock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class V4TodaySummary:
    decision_window_et: str
    settlement_window_et: str
    deadline_et: str
    events_in_window: int
    business_eligible: int
    research_ready: int
    waiting_decision: int
    decisions_today: int
    ranked_today: int
    no_action_today: int
    entries_observed_today: int
    entries_failed_today: int
    deadline_skipped_today: int
    research_not_ready_today: int
    settlements_due_today: int
    settled_today: int
    settlements_failed_today: int


def compute_today_summary(
    db: Session, pipeline: list[V4PipelineEvent], *, now: datetime | None = None
) -> V4TodaySummary:
    now = now or datetime.now(UTC)
    today = now.astimezone(EASTERN).date()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=EASTERN)
    todays = [p for p in pipeline if p.entry_timestamp.astimezone(EASTERN).date() == today]
    eligible = [p for p in todays if p.lifecycle_state != STATE_BUSINESS_INELIGIBLE]
    decisions = db.query(V4ShadowDecision).filter(V4ShadowDecision.generated_at >= day_start).all()
    entries = (
        db.query(V4ShadowConfigEntry).filter(V4ShadowConfigEntry.observed_at >= day_start).all()
    )
    settlements = (
        db.query(V4ShadowConfigSettlement)
        .filter(V4ShadowConfigSettlement.settled_at >= day_start)
        .all()
    )
    run_events = db.query(V4ShadowRunEvent).filter(V4ShadowRunEvent.occurred_at >= day_start).all()
    return V4TodaySummary(
        decision_window_et=V4_ACTIVE_TIMING_POLICY.entry_time.strftime("%H:%M"),
        settlement_window_et=V4_ACTIVE_TIMING_POLICY.exit_time.strftime("%H:%M"),
        deadline_et=DECISION_DEADLINE_ET.strftime("%H:%M"),
        events_in_window=len(todays),
        business_eligible=len(eligible),
        research_ready=sum(1 for p in eligible if p.research_ready),
        waiting_decision=sum(1 for p in eligible if p.lifecycle_state == STATE_WAITING_DECISION),
        decisions_today=len(decisions),
        ranked_today=sum(1 for d in decisions if d.status == "RANKED"),
        no_action_today=sum(1 for d in decisions if d.status == "NO_ACTION"),
        entries_observed_today=sum(1 for e in entries if e.status == "OBSERVED"),
        entries_failed_today=sum(1 for e in entries if e.status != "OBSERVED"),
        deadline_skipped_today=sum(1 for e in run_events if e.category == "DEADLINE_SKIPPED"),
        research_not_ready_today=sum(1 for e in run_events if e.category == "RESEARCH_NOT_READY"),
        settlements_due_today=sum(
            1
            for p in pipeline
            if p.lifecycle_state == STATE_WAITING_SETTLEMENT
            and p.exit_timestamp.astimezone(EASTERN).date() == today
        ),
        settled_today=sum(1 for s in settlements if s.status == "SETTLED"),
        settlements_failed_today=sum(1 for s in settlements if s.status != "SETTLED"),
    )


ZURICH = ZoneInfo("Europe/Zurich")


@dataclass(frozen=True)
class MarketClock:
    utc_now: datetime
    new_york_now: datetime
    zurich_now: datetime
    market_session: str
    next_automatic_action_job_id: str | None
    next_automatic_action_at: datetime | None
    settlement_window_tolerance_minutes: int = int(EARLY_CAPTURE_TOLERANCE.total_seconds() // 60)


def get_market_clock(
    scheduler_status: SchedulerStatus, *, now: datetime | None = None
) -> MarketClock:
    now = now or datetime.now(UTC)
    session = get_market_session(now)
    upcoming = [
        (j.job_id, j.next_run_time) for j in scheduler_status.jobs if j.next_run_time is not None
    ]
    next_job_id, next_at = min(upcoming, key=lambda pair: pair[1]) if upcoming else (None, None)
    return MarketClock(
        utc_now=now,
        new_york_now=now.astimezone(EASTERN),
        zurich_now=now.astimezone(ZURICH),
        market_session=session.session.value,
        next_automatic_action_job_id=next_job_id,
        next_automatic_action_at=next_at,
    )


# ---------------------------------------------------------------------------
# The 15:30 ET forward window (settlement-priority hardening, v4.0.0)
# ---------------------------------------------------------------------------

FORWARD_WINDOW_PRIORITY = ("Due settlements", "New decision observations")


@dataclass(frozen=True)
class ForwardWindowStatus:
    """What the next 15:30 ET window will do, in execution order, and how the
    last one went -- from the pipeline read model and the persisted
    forward-window telemetry, never estimated."""

    window_time_et: str
    priority: tuple[str, ...]
    next_window_at: datetime | None
    settlements_due: tuple[str, ...]
    decisions_ready: tuple[str, ...]
    decisions_not_ready: tuple[str, ...]
    last_window_started_at: datetime | None
    last_settlements_due: int
    last_settlements_settled: int
    last_settlements_failed: int
    last_settlements_window_missed: int
    last_settlement_lock_wait_ms_max: int | None
    last_settlement_total_ms_max: int | None
    last_decisions_ready: int
    last_deadline_skipped: int
    last_decision_lock_wait_ms: int | None


def compute_forward_window(
    db: Session, pipeline: list[V4PipelineEvent], *, now: datetime | None = None
) -> ForwardWindowStatus:
    now = now or datetime.now(UTC)
    candidates = [
        (p.exit_timestamp, p)
        for p in pipeline
        if p.lifecycle_state == STATE_WAITING_SETTLEMENT and p.exit_timestamp >= now
    ] + [
        (p.entry_timestamp, p)
        for p in pipeline
        if p.lifecycle_state
        in (
            STATE_WAITING_DECISION,
            STATE_RESEARCH_QUEUED,
            STATE_RESEARCH_RUNNING,
            STATE_RESEARCH_READY,
            STATE_CALENDAR_DISCOVERED,
        )
        and p.entry_timestamp >= now - LATE_CUTOFF_GRACE
    ]
    next_at = min((t for t, _ in candidates), default=None)
    window_date = next_at.astimezone(EASTERN).date() if next_at else None
    settlements = tuple(
        sorted(
            p.symbol
            for t, p in candidates
            if p.lifecycle_state == STATE_WAITING_SETTLEMENT
            and t.astimezone(EASTERN).date() == window_date
        )
    )
    decision_rows = [
        p
        for t, p in candidates
        if p.lifecycle_state != STATE_WAITING_SETTLEMENT
        and t.astimezone(EASTERN).date() == window_date
    ]
    ready = tuple(sorted(p.symbol for p in decision_rows if p.research_ready))
    not_ready = tuple(sorted(p.symbol for p in decision_rows if not p.research_ready))

    latest_phase = (
        db.query(V4ForwardWindowTelemetry)
        .filter(V4ForwardWindowTelemetry.shadow_decision_id.is_(None))
        .order_by(V4ForwardWindowTelemetry.job_started_at.desc().nullslast())
        .first()
    )
    last_started = latest_phase.job_started_at if latest_phase else None
    rows: list[V4ForwardWindowTelemetry] = []
    if last_started is not None:
        rows = (
            db.query(V4ForwardWindowTelemetry)
            .filter(V4ForwardWindowTelemetry.job_started_at == last_started)
            .all()
        )
    settle_rows = [r for r in rows if r.phase == "settlement" and r.shadow_decision_id is not None]
    decision_phase = next(
        (r for r in rows if r.phase == "decision" and r.shadow_decision_id is None), None
    )
    decision_outcome = _parse_outcome(decision_phase.detail if decision_phase else "")
    return ForwardWindowStatus(
        window_time_et=V4_ACTIVE_TIMING_POLICY.entry_time.strftime("%H:%M"),
        priority=FORWARD_WINDOW_PRIORITY,
        next_window_at=next_at,
        settlements_due=settlements,
        decisions_ready=ready,
        decisions_not_ready=not_ready,
        last_window_started_at=last_started,
        last_settlements_due=len(settle_rows),
        last_settlements_settled=sum(1 for r in settle_rows if r.outcome == "settled"),
        last_settlements_failed=sum(
            1 for r in settle_rows if r.outcome in ("failed", "partially_failed", "error")
        ),
        last_settlements_window_missed=sum(1 for r in settle_rows if r.outcome == "window_missed"),
        last_settlement_lock_wait_ms_max=max(
            (r.lock_wait_ms for r in settle_rows if r.lock_wait_ms is not None), default=None
        ),
        last_settlement_total_ms_max=max(
            (r.total_ms for r in settle_rows if r.total_ms is not None), default=None
        ),
        last_decisions_ready=decision_outcome.get("ranked", 0)
        + decision_outcome.get("no_action", 0)
        + decision_outcome.get("failed", 0)
        + decision_outcome.get("deadline_skipped", 0),
        last_deadline_skipped=decision_outcome.get("deadline_skipped", 0),
        last_decision_lock_wait_ms=decision_phase.lock_wait_ms if decision_phase else None,
    )


def _parse_outcome(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for token in (text or "").split():
        if "=" in token:
            key, _, value = token.partition("=")
            if value.isdigit():
                out[key] = int(value)
    return out
