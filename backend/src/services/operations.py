"""Live Operations Monitor -- read-only aggregation over real, already-
persisted state. Nothing here computes a NEW decision, eligibility
verdict, price, or P&L -- every value is either read directly from an
existing table (EarningsCalendarEvent, DecisionSnapshot,
EntryCaptureAttempt, SettlementCaptureAttempt, ProviderHealthEvent,
SchedulerRun/SchedulerRunEvent) or derived via pure, side-effect-free
computation over those rows (analytics/earnings_timing.py's own
schedule math, mostly).

Deliberately never makes a live IBKR/EarningsAPI/LLM call to check
"is this eligible right now" for the pipeline view -- that's exactly
what the real scheduler jobs already do once a day, at real cost against
real rate limits (see services/scheduler_run_tracking.py), and refreshing
this page every ~15 seconds must not multiply that cost. Eligibility
shown here is always "as of the most recent real scheduler run that
evaluated this event" -- honestly labeled "not yet evaluated" when no
run has touched it yet, never guessed live.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from analytics.decision.v4_methodology import V4_METHODOLOGY
from analytics.earnings_timing import EarningsEntryExitSchedule, compute_entry_exit_schedule
from analytics.market_session import EASTERN, get_market_session
from core.config import Settings
from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.enums import (
    AnnouncementTime,
    CaptureStatus,
    EarningsTiming,
)
from models.provider_health_event import ProviderHealthEvent
from models.research_preparation_job import (
    STEP_LABELS,
    JobStatus,
    PreparationStep,
    ResearchPreparationJob,
)
from models.scheduler_run import SchedulerRun, SchedulerRunEvent
from models.settlement_capture_attempt import SettlementCaptureAttempt
from providers.ibkr_client import IBKRClient
from providers.ibkr_tws_health import TwsHealthProbe
from providers.ibkr_tws_options import IBKRTWSProvider
from services.decision_pipeline import LATE_CUTOFF_GRACE
from services.provider_status import get_provider_dashboard
from services.research_preparation_queue import count_queue_depth
from services.scheduler import (
    CALENDAR_SYNC_JOB_ID,
    DECISION_AND_ENTRY_CAPTURE_JOB_ID,
    EARNINGS_RESEARCH_PREPARATION_JOB_ID,
    EXIT_CAPTURE_JOB_ID,
    IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
    SchedulerStatus,
)
from services.scheduler_run_tracking import (
    FAILURE_OUTCOMES,
    OUTCOME_DECISION_NO_ACTION,
    OUTCOME_ENTRY_CAPTURED,
    OUTCOME_ENTRY_FAILED,
    OUTCOME_SETTLEMENT_CAPTURED,
    OUTCOME_SETTLEMENT_FAILED,
)
from services.system_status import IbkrStatus, get_ibkr_status, get_tws_status

# Same BMO/AMC/DMH -> AnnouncementTime mapping services/scheduler.py
# itself already imports from services/benchmark_exit_capture.py -- kept
# as a local copy rather than importing that module's private helper a
# second time from here, since this file has no other reason to depend
# on the exit-capture service.
_TIMING_TO_ANNOUNCEMENT_TIME: dict[EarningsTiming, AnnouncementTime] = {
    EarningsTiming.BMO: AnnouncementTime.BEFORE_MARKET,
    EarningsTiming.AMC: AnnouncementTime.AFTER_MARKET,
    EarningsTiming.DMH: AnnouncementTime.UNKNOWN,
    EarningsTiming.UNKNOWN: AnnouncementTime.UNKNOWN,
}

# How far forward the "Today's Pipeline" view looks -- wide enough to
# show the next several real decision/entry windows (each on its own
# real day), never the whole multi-month calendar.
PIPELINE_WINDOW_DAYS = 7

# Lifecycle states (Section 4) -- a pure label derived from real rows,
# never written back to any table.
STATE_NOT_ELIGIBLE = "NOT_ELIGIBLE"
STATE_WAITING_FOR_DECISION = "WAITING_FOR_DECISION"
STATE_DECISION_GENERATED = "DECISION_GENERATED"
STATE_WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
STATE_ENTRY_CAPTURED = "ENTRY_CAPTURED"
STATE_ENTRY_FAILED = "ENTRY_FAILED"
# Post-live correction (2026-08-25): a real, terminal, non-error outcome
# -- the strategy engine looked at this event and genuinely found no
# actionable strategy (services/decision_engine.py::generate_decision's
# own ``recommended is None`` case, frozen with legs=None by services/
# decision_snapshot_freezing.py -- see that module's own docstring).
# Distinct from ENTRY_FAILED, which means a real strategy existed but
# capturing its market price failed -- a no-action decision is not an
# infrastructure failure and must never be counted or displayed as one.
STATE_NO_ACTION = "NO_ACTION"
STATE_WAITING_FOR_SETTLEMENT = "WAITING_FOR_SETTLEMENT"
STATE_SETTLED = "SETTLED"
STATE_SETTLEMENT_FAILED = "SETTLEMENT_FAILED"
STATE_SKIPPED = "SKIPPED"
STATE_CALENDAR_DISCOVERED = "CALENDAR_DISCOVERED"
# Pre-live hardening (2026-08-25) -- automatic research preparation
# states, real only for an event not yet due for decision (see
# derive_lifecycle_state's own preparation branch below). Never a
# DecisionSnapshot-derived state: these come entirely from stage=
# "preparation" SchedulerRunEvent rows (services/earnings_research_
# preparation.py), a separate observability trail from the decision
# stage's own events.
STATE_FILTERED_OUT = "FILTERED_OUT"
STATE_READY_FOR_DECISION = "READY_FOR_DECISION"
STATE_PREPARATION_FAILED = "PREPARATION_FAILED"

_HEALTHY = "green"
_DEGRADED = "yellow"
_FAILED = "red"
_NOT_APPLICABLE = "gray"


def _timing_to_announcement(timing: EarningsTiming) -> AnnouncementTime:
    return _TIMING_TO_ANNOUNCEMENT_TIME[timing]


# --------------------------------------------------------------------------
# Section 4 -- lifecycle state derivation. Pure function: every argument
# is a real, already-fetched value (or None); no DB/network access here
# at all, so this is trivially unit-testable and safe to call per-event
# in a loop without any N+1/rate-limit concern.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleResult:
    state: str
    reason: str | None
    next_action: str | None
    next_action_at: datetime | None


def derive_lifecycle_state(
    *,
    schedule: EarningsEntryExitSchedule,
    now: datetime,
    latest_decision_outcome: str | None,
    latest_decision_reason: str | None,
    decision_snapshot: DecisionSnapshot | None,
    latest_entry_attempt: EntryCaptureAttempt | None,
    latest_settlement_attempt: SettlementCaptureAttempt | None,
    latest_preparation_outcome: str | None = None,
    latest_preparation_reason: str | None = None,
) -> LifecycleResult:
    """``latest_decision_outcome``/``reason`` come from the most recent
    real SchedulerRunEvent (stage="decision") for this calendar event, if
    any -- "not yet evaluated" (state CALENDAR_DISCOVERED) is an honest,
    real answer when no scheduler run has looked at this event yet,
    never guessed via a live eligibility call from this read path.

    Post-live correction (2026-08-25): two real bugs found in the Aug 25
    forward-test run, both fixed here --

    1. A no-action decision (the strategy engine genuinely recommended
       nothing -- decision_snapshot.legs is empty, see
       services/decision_snapshot_freezing.py) was indistinguishable from
       a real capture failure, because capture_benchmark_entry itself
       records a FAILED EntryCaptureAttempt for both cases. Checked here
       directly against decision_snapshot.legs -- the same authoritative
       signal that module itself uses -- rather than pattern-matching the
       attempt's error text, and checked before the generic entry-FAILED
       branch so it takes priority.
    2. A hard preparation-time filter (STATE_FILTERED_OUT) was checked
       only *after* the "now >= schedule.entry_timestamp" branch, so any
       filtered event whose entry_timestamp had already passed (every
       BMO event, by the afternoon) silently reverted to
       STATE_WAITING_FOR_DECISION -- a terminal rejection must never
       un-terminal itself just because time passed. Moved above that
       check. STATE_PREPARATION_FAILED deliberately stays where it was:
       unlike a hard filter, a preparation failure doesn't preclude the
       decision job's own later, independent eligibility/data check, so
       it must still be allowed to progress to WAITING_FOR_DECISION.
    """
    if latest_settlement_attempt is not None:
        if latest_settlement_attempt.status == CaptureStatus.CAPTURED:
            return LifecycleResult(STATE_SETTLED, None, None, None)
        if latest_settlement_attempt.status == CaptureStatus.FAILED:
            return LifecycleResult(
                STATE_SETTLEMENT_FAILED,
                latest_settlement_attempt.capture_error,
                "Retry settlement capture",
                None,
            )

    if latest_entry_attempt is not None and latest_entry_attempt.status == CaptureStatus.CAPTURED:
        if now < schedule.exit_timestamp:
            return LifecycleResult(
                STATE_WAITING_FOR_SETTLEMENT,
                None,
                "Capture settlement",
                schedule.exit_timestamp,
            )
        return LifecycleResult(
            STATE_WAITING_FOR_SETTLEMENT, None, "Capture settlement (due)", schedule.exit_timestamp
        )

    if decision_snapshot is not None and not decision_snapshot.legs:
        return LifecycleResult(
            STATE_NO_ACTION,
            "the strategy engine found no actionable strategy for this event",
            None,
            None,
        )

    if latest_entry_attempt is not None and latest_entry_attempt.status == CaptureStatus.FAILED:
        entry_window_closed = now > schedule.entry_timestamp + LATE_CUTOFF_GRACE
        return LifecycleResult(
            STATE_ENTRY_FAILED,
            latest_entry_attempt.capture_error,
            None if entry_window_closed else "Retry entry capture",
            None,
        )

    if decision_snapshot is not None:
        if now < schedule.entry_timestamp:
            return LifecycleResult(
                STATE_DECISION_GENERATED, None, "Capture entry", schedule.entry_timestamp
            )
        return LifecycleResult(
            STATE_WAITING_FOR_ENTRY, None, "Capture entry (due)", schedule.entry_timestamp
        )

    if latest_decision_outcome == "skipped_ineligible":
        return LifecycleResult(STATE_NOT_ELIGIBLE, latest_decision_reason, None, None)
    if latest_decision_outcome == "contract_resolution_failed":
        # V4 consolidation, Section 14 -- a provider/transport failure is
        # NOT a business judgement about the company. It is shown as a
        # failure with the provider's own reason, and remains retryable.
        return LifecycleResult(
            STATE_SKIPPED,
            f"Contract resolution failed: {latest_decision_reason}",
            "Retry at next window",
            None,
        )
    if latest_decision_outcome == "skipped_no_company":
        return LifecycleResult(STATE_SKIPPED, latest_decision_reason, None, None)
    if latest_decision_outcome == "failed":
        return LifecycleResult(
            STATE_SKIPPED,
            latest_decision_reason,
            "Investigate decision generation failure",
            None,
        )

    # A hard preparation-time filter is terminal -- checked before the
    # due-time branch below so it can never revert to "waiting" just
    # because the entry_timestamp has since passed (see this function's
    # own docstring, point 2).
    if latest_preparation_outcome == "filtered_out":
        return LifecycleResult(STATE_FILTERED_OUT, latest_preparation_reason, None, None)

    if now >= schedule.entry_timestamp:
        # Due (or past due) for decision generation, but no real
        # DecisionSnapshot exists yet and no scheduler run has recorded
        # an ineligible/failed verdict either -- honestly "waiting", not
        # "failed": the next scheduled run will attempt it (or, if the
        # window has already fully closed, decision_pipeline.py's own
        # no-lookahead check will record skipped_too_late on its next
        # attempt, which this function will then show as SKIPPED).
        return LifecycleResult(
            STATE_WAITING_FOR_DECISION,
            None,
            "Generate decision + capture entry",
            schedule.entry_timestamp,
        )

    # Not yet due for decision -- the honest current state is whatever
    # the most recent real preparation-stage SchedulerRunEvent says (see
    # services/earnings_research_preparation.py), never guessed live.
    # "Not yet touched by preparation at all" (CALENDAR_DISCOVERED) is
    # itself a real, distinct answer from "preparation ran and rejected
    # it" (FILTERED_OUT, checked above) or "preparation ran and failed"
    # (PREPARATION_FAILED) -- this must never collapse those into one
    # generic state.
    if latest_preparation_outcome == "preparation_failed":
        return LifecycleResult(
            STATE_PREPARATION_FAILED,
            latest_preparation_reason,
            "Retry research preparation",
            None,
        )
    if latest_preparation_outcome in ("prepared", "already_prepared"):
        return LifecycleResult(
            STATE_READY_FOR_DECISION,
            None,
            "Generate decision + capture entry",
            schedule.entry_timestamp,
        )

    return LifecycleResult(
        STATE_CALENDAR_DISCOVERED,
        None,
        "Generate decision + capture entry",
        schedule.entry_timestamp,
    )


# --------------------------------------------------------------------------
# Section 2 -- top-level system health.
# --------------------------------------------------------------------------


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
    # IBKR TWS Migration, Phase 3 readiness (Section 13) -- which real
    # transport this snapshot reflects ("web" or "tws", from settings.
    # ibkr_provider), so the Operations page can show the active provider
    # truthfully once a future cutover happens rather than always
    # implying Web. Purely additive: every field above keeps its exact
    # pre-Phase-3 meaning for "web" (the only value this project sets
    # today); see get_system_health's own comment for the "tws" mapping.
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
class SystemHealth:
    ibkr: IbkrHealth
    earnings_calendar: EarningsCalendarHealth
    ai_provider: AiProviderHealth
    scheduler: SchedulerHealth
    database: DatabaseHealth
    #: V4.4C (Sections 51/80) -- a SEPARATE health domain. A V4 shadow
    #: failure must never mark the official V3 system unhealthy: shadow
    #: generation is experimental, isolated, and its degradation has no
    #: bearing on whether the official forward test can run. Kept
    #: optional so every existing caller and test constructing a
    #: SystemHealth without it is unaffected.
    v4_shadow: "V4ShadowHealth | None" = None


@dataclass(frozen=True)
class V4ShadowHealth:
    """Experimental-cohort health, reported alongside but never merged
    into the official domains above."""

    state: str
    enabled: bool
    decisions_today: int
    ranked_today: int
    no_action_today: int
    failed_today: int
    entry_observations_failed_today: int
    settlements_due: int
    settlements_complete: int
    last_shadow_run_at: datetime | None
    engine_version: str | None
    note: str


# Live vs. paper is a static property of which real credentials are
# configured -- it never flips mid-session -- so the one extra real
# IBKR call needed to know it (GET /iserver/accounts, beyond what
# get_ibkr_status() already makes) is cached for a few minutes rather
# than repeated on every ~15s Operations page poll (Section 10's own
# "don't hammer IBKR" concern). A real cache miss still makes a real,
# live call; this only avoids a redundant one every single refresh.
_LIVE_ACCOUNT_CACHE_TTL = timedelta(minutes=5)
_live_account_cache: tuple[datetime, bool | None] | None = None


def _is_live_ibkr_account(settings: Settings, ibkr_status: IbkrStatus) -> bool | None:
    """A real, additional read-only check against the already-existing,
    already-authenticated Gateway session (GET /iserver/accounts) --
    confirmed live: a real IBKR account id starts with "U" for a live
    account, "DU" for paper, and the same real response also carries an
    explicit "isPaper" boolean, which is what's actually used here (more
    authoritative than parsing a prefix). Never reads, logs, or returns
    the account id itself -- only the one real boolean this page needs.
    Returns None (never guessed) when the Gateway isn't reachable/
    authenticated at all, matching every other "not applicable" signal
    on this page."""
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
    except Exception:
        # A real, live extra IBKR call this page makes -- any failure
        # here must never break the whole Operations page, only leave
        # this one field honestly unknown (and uncached, so the next
        # poll tries again rather than freezing on a transient error).
        return None
    _live_account_cache = (now, result)
    return result



def get_v4_shadow_health(db: Session, settings: Settings) -> V4ShadowHealth:
    """V4.4C (Sections 51/81) -- experimental-cohort counts, deliberately
    kept out of the official execution summary so shadow activity can
    never contaminate or distort official V3 reporting.

    The state vocabulary is intentionally narrow: DISABLED when the
    activation flag is off (the default and the current production
    state), DEGRADED when shadow work failed, GREEN otherwise. There is
    no CRITICAL -- a shadow failure is never a production emergency
    (Section 80)."""
    from models.v4_shadow import V4ShadowDecision, V4ShadowObservation, V4ShadowSettlement

    if not settings.v4_shadow_enabled:
        return V4ShadowHealth(
            state="gray",
            enabled=False,
            decisions_today=0,
            ranked_today=0,
            no_action_today=0,
            failed_today=0,
            entry_observations_failed_today=0,
            settlements_due=0,
            settlements_complete=0,
            last_shadow_run_at=None,
            engine_version=None,
            note="V4 shadow generation is DISABLED (V4_SHADOW_ENABLED=false)",
        )

    today_start = datetime.now(UTC) - timedelta(hours=24)
    base = db.query(V4ShadowDecision).filter(V4ShadowDecision.generated_at >= today_start)
    decisions_today = base.count()
    ranked = base.filter(V4ShadowDecision.status == "RANKED").count()
    no_action = base.filter(V4ShadowDecision.status == "NO_ACTION").count()
    failed = base.filter(V4ShadowDecision.status == "FAILED").count()
    entry_failed = (
        db.query(V4ShadowObservation)
        .filter(
            V4ShadowObservation.phase == "ENTRY",
            V4ShadowObservation.status != "OBSERVED",
            V4ShadowObservation.observed_at >= today_start,
        )
        .count()
    )
    settled = db.query(V4ShadowSettlement).filter_by(status="SETTLED").count()
    # Due = a ranked decision with an entry observation but no settlement yet.
    settlements_due = (
        db.query(V4ShadowDecision)
        .filter(V4ShadowDecision.status == "RANKED")
        .filter(
            ~V4ShadowDecision.id.in_(db.query(V4ShadowSettlement.shadow_decision_id))
        )
        .count()
    )
    last_run = (
        db.query(func.max(V4ShadowDecision.generated_at)).scalar()
    )

    # A NO_ACTION is an outcome, never a failure (Section 54) -- only real
    # failures degrade this domain.
    state = "yellow" if (failed or entry_failed) else "green"
    return V4ShadowHealth(
        state=state,
        enabled=True,
        decisions_today=decisions_today,
        ranked_today=ranked,
        no_action_today=no_action,
        failed_today=failed,
        entry_observations_failed_today=entry_failed,
        settlements_due=settlements_due,
        settlements_complete=settled,
        last_shadow_run_at=last_run,
        engine_version=V4_METHODOLOGY.engine_version,
        note=(
            "EXPERIMENTAL SHADOW COHORT -- separate health domain; a shadow failure never "
            "marks the official V3 system unhealthy"
        ),
    )

def _last_provider_error(db: Session, provider: str, domain: str) -> ProviderHealthEvent | None:
    return (
        db.query(ProviderHealthEvent)
        .filter(ProviderHealthEvent.provider == provider, ProviderHealthEvent.domain == domain)
        .order_by(ProviderHealthEvent.occurred_at.desc())
        .first()
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
    """A real `SELECT 1` (mirroring api/routers/health.py's own `/ready`
    check) plus the real `alembic_version` row -- never a hardcoded
    "healthy". Reaching this function at all already proves the backend
    process is up and serving requests, so `backend_healthy` is a real,
    non-fabricated fact rather than a guess; `database_healthy` and
    `migration_head` are not, and must be checked directly."""
    try:
        db.execute(text("SELECT 1"))
        database_healthy = True
    except Exception:
        return DatabaseHealth(
            state=_FAILED, backend_healthy=True, database_healthy=False, migration_head=None
        )
    try:
        migration_head = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        migration_head = None
    return DatabaseHealth(
        state=_HEALTHY if database_healthy else _FAILED,
        backend_healthy=True,
        database_healthy=database_healthy,
        migration_head=migration_head,
    )


def get_system_health(
    db: Session,
    settings: Settings,
    scheduler_status: SchedulerStatus,
    tws_health_probe: TwsHealthProbe | None = None,
    tws_provider: IBKRTWSProvider | None = None,
) -> SystemHealth:
    """IBKR TWS Migration, Phase 3 readiness (Section 13) -- provider-
    aware, additive-only: the "web" branch below is byte-for-byte the
    pre-Phase-3 logic (real default, unchanged). The "tws" branch reuses
    ``tws_health_probe`` -- the SAME shared, long-lived TwsHealthProbe
    api/main.py's lifespan already owns for /system-status and the
    scheduler healthcheck job (see services/scheduler.py::run_ibkr_
    gateway_healthcheck_job's own docstring) -- so polling this endpoint
    never opens an additional TWS connection, matching this migration's
    own "Operations polling must not create additional connections" rule.
    No live-account concept exists for TWS in this codebase yet (no
    reqAccountSummary-based check has been built) -- reported honestly as
    ``None`` (unknown), never guessed, exactly this ABC's "unsupported"
    convention elsewhere in this migration.
    """
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
    # A real DecisionSnapshot row is undeniable proof a real AI
    # generation succeeded -- a stronger, more directly relevant signal
    # than provider_status.py's own "Test Connection" event (its own
    # docstring already notes no persisted artifact of a successful
    # generation exists at the provider-status layer; this page has one).
    last_decision_generated_at = db.query(func.max(DecisionSnapshot.generated_at)).scalar()
    decision_run = _latest_scheduler_run(db, DECISION_AND_ENTRY_CAPTURE_JOB_ID)
    decision_run_error_summary = (
        decision_run.error_summary
        if decision_run is not None and decision_run.status == "error"
        else None
    )
    ai_state = _NOT_APPLICABLE
    if ai_active is not None:
        ai_state = _HEALTHY if ai_active.configured else _DEGRADED
        if ai_error_event is not None or decision_run_error_summary is not None:
            ai_state = _DEGRADED
    ai_provider = AiProviderHealth(
        state=ai_state,
        provider=ai_provider_name or "unknown",
        configured=bool(ai_active and ai_active.configured),
        last_successful_generation_at=last_decision_generated_at,
        last_error=decision_run_error_summary
        or (ai_error_event.detail if ai_error_event else None),
    )

    scheduler_state = _HEALTHY if scheduler_status.running else _FAILED
    scheduler = SchedulerHealth(
        state=scheduler_state,
        running=scheduler_status.running,
        registered_job_count=len(scheduler_status.jobs),
        last_activity_at=max(
            (j.last_run_at for j in scheduler_status.jobs if j.last_run_at is not None),
            default=None,
        ),
        next_activity_at=min(
            (j.next_run_time for j in scheduler_status.jobs if j.next_run_time is not None),
            default=None,
        ),
    )

    database = _get_database_health(db)

    return SystemHealth(
        ibkr=ibkr,
        earnings_calendar=earnings_calendar,
        ai_provider=ai_provider,
        scheduler=scheduler,
        database=database,
        # V4.4C -- a SEPARATE domain (Sections 51/80). Deliberately the
        # last field and optional: a shadow failure is reported here and
        # nowhere else, so it can never degrade an official domain above
        # or change how the official pipeline is judged healthy.
        v4_shadow=get_v4_shadow_health(db, settings),
    )


# --------------------------------------------------------------------------
# Section 3/4/5 -- Today's Pipeline: real events, real derived lifecycle,
# real timeline. One bulk query per real table, never one query per
# event (Section 17's own "avoid N+1" requirement).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineStep:
    label: str
    at: datetime | None
    status: str  # "done" | "pending" | "failed" | "warning"
    detail: str | None = None


@dataclass(frozen=True)
class PipelineEvent:
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
    decision_snapshot_id: int | None
    entry_capture_attempt_id: int | None
    settlement_capture_attempt_id: int | None
    timeline: list[TimelineStep] = field(default_factory=list)


def _latest_by_key(rows: list, key_attr: str, order_attr: str) -> dict:
    """The real "most recent row per group" reduction used for entry/
    settlement attempts (retries are new rows, never updates -- see
    models/entry_capture_attempt.py) and scheduler_run_event outcomes.
    Pure Python, not a window-function query: the per-window row counts
    this page deals with (a handful of days of real events) make that a
    real, deliberate simplicity trade-off, not a performance risk."""
    latest: dict = {}
    for row in rows:
        key = getattr(row, key_attr)
        order_value = getattr(row, order_attr)
        current = latest.get(key)
        if current is None or order_value > getattr(current, order_attr):
            latest[key] = row
    return latest


_READY_JOB_STATUSES = (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS)


def _effective_preparation_status(
    job: ResearchPreparationJob | None, prep_event: SchedulerRunEvent | None
) -> tuple[str | None, str | None, datetime | None]:
    """The real, current "has research actually finished for this event"
    answer -- see get_todays_pipeline's own comment on why a preparation-
    stage SchedulerRunEvent alone can no longer answer this. A queue-
    managed job row (if one exists) is authoritative and always wins;
    its still-in-progress statuses (PENDING/RUNNING/INTERRUPTED)
    deliberately report as "not yet resolved" (None) rather than
    guessing -- the honest CALENDAR_DISCOVERED/"pending" state, not a
    fabricated in-between one. Falling back to the preparation event only
    covers "filtered_out"/"preparation_warning" (neither ever gets a job
    row -- see services/earnings_research_preparation.py::
    enqueue_preparation_candidates)."""
    if job is not None:
        if job.status in _READY_JOB_STATUSES:
            return "prepared", None, job.completed_at or job.started_at
        if job.status == JobStatus.FAILED:
            return "preparation_failed", job.error, job.completed_at or job.started_at
        return None, None, job.started_at
    if prep_event is not None:
        return prep_event.outcome, prep_event.reason, prep_event.occurred_at
    return None, None, None


def _build_timeline(
    *,
    event: EarningsCalendarEvent,
    schedule: EarningsEntryExitSchedule,
    preparation_outcome: str | None,
    preparation_reason: str | None,
    preparation_occurred_at: datetime | None,
    latest_decision_run_event: SchedulerRunEvent | None,
    decision_snapshot: DecisionSnapshot | None,
    latest_entry_attempt: EntryCaptureAttempt | None,
    latest_settlement_attempt: SettlementCaptureAttempt | None,
) -> list[TimelineStep]:
    steps: list[TimelineStep] = []
    steps.append(
        TimelineStep(
            "Earnings event synced",
            event.created_at,
            "done",
            f"Source: {event.source.value}",
        )
    )
    if preparation_outcome is not None:
        # "done" if the outcome was positive, "failed" (red) for a real,
        # permanent rejection -- filtered_out shows as "failed", matching
        # how skipped_ineligible already reads there, not because a
        # cheap-filter rejection is an error, but for a consistent
        # visual language across the whole timeline. Post-live correction
        # (2026-08-25): a transient, non-blocking failure (preparation_
        # warning -- see services/earnings_eligibility.py::
        # EligibilityResult.retryable's own docstring for the real Aug 25
        # WSM evidence this exists to fix) shows as "warning" (amber)
        # instead -- it did not, and must not appear to, block the
        # pipeline the way a genuine fatal preparation failure does.
        prepared = preparation_outcome in ("prepared", "already_prepared")
        if prepared:
            status = "done"
        elif preparation_outcome == "preparation_warning":
            status = "warning"
        else:
            status = "failed"
        steps.append(
            TimelineStep(
                "Research prepared",
                preparation_occurred_at,
                status,
                preparation_reason,
            )
        )
    else:
        steps.append(TimelineStep("Research prepared", None, "pending"))
    if latest_decision_run_event is not None:
        eligible = latest_decision_run_event.outcome not in (
            "skipped_ineligible", "contract_resolution_failed", "failed"
        )
        steps.append(
            TimelineStep(
                "Eligibility verified",
                latest_decision_run_event.occurred_at,
                "done" if eligible else "failed",
                latest_decision_run_event.reason,
            )
        )
    else:
        steps.append(TimelineStep("Eligibility verified", None, "pending"))

    if decision_snapshot is not None:
        steps.append(TimelineStep("Decision generated", decision_snapshot.generated_at, "done"))
        steps.append(TimelineStep("DecisionSnapshot frozen", decision_snapshot.created_at, "done"))
    else:
        steps.append(
            TimelineStep(
                "Decision generated",
                None,
                "pending",
                f"Scheduled: {schedule.entry_timestamp.isoformat()}",
            )
        )

    if latest_entry_attempt is not None:
        entry_done = latest_entry_attempt.status == CaptureStatus.CAPTURED
        steps.append(
            TimelineStep(
                "IBKR entry capture",
                latest_entry_attempt.captured_at or latest_entry_attempt.created_at,
                "done" if entry_done else "failed",
                latest_entry_attempt.capture_error,
            )
        )
        if entry_done:
            steps.append(
                TimelineStep("EntrySnapshot persisted", latest_entry_attempt.captured_at, "done")
            )
    elif decision_snapshot is not None:
        steps.append(TimelineStep("IBKR entry capture", None, "pending"))

    if latest_settlement_attempt is not None:
        settle_done = latest_settlement_attempt.status == CaptureStatus.CAPTURED
        steps.append(
            TimelineStep(
                "Settlement capture",
                latest_settlement_attempt.captured_at or latest_settlement_attempt.created_at,
                "done" if settle_done else "failed",
                latest_settlement_attempt.capture_error,
            )
        )
    elif latest_entry_attempt is not None and latest_entry_attempt.status == CaptureStatus.CAPTURED:
        steps.append(
            TimelineStep(
                "Settlement scheduled", None, "pending", f"{schedule.exit_timestamp.isoformat()}"
            )
        )
    return steps


def get_todays_pipeline(
    db: Session, *, now: datetime | None = None, window_days: int = PIPELINE_WINDOW_DAYS
) -> list[PipelineEvent]:
    now = now or datetime.now(UTC)
    today_et = now.astimezone(EASTERN).date()
    window_start = today_et - timedelta(days=3)
    window_end = today_et + timedelta(days=window_days)

    events = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.earnings_date >= window_start,
            EarningsCalendarEvent.earnings_date <= window_end,
        )
        .order_by(EarningsCalendarEvent.market_cap.desc().nullslast())
        .all()
    )
    if not events:
        return []
    event_ids = [e.id for e in events]

    decisions = (
        db.query(DecisionSnapshot)
        .filter(DecisionSnapshot.earnings_calendar_event_id.in_(event_ids))
        .all()
    )
    decision_by_event_id = {d.earnings_calendar_event_id: d for d in decisions}
    decision_ids = [d.id for d in decisions]

    entry_attempts = (
        db.query(EntryCaptureAttempt)
        .filter(EntryCaptureAttempt.decision_snapshot_id.in_(decision_ids))
        .all()
        if decision_ids
        else []
    )
    latest_entry_by_decision_id = _latest_by_key(
        entry_attempts, "decision_snapshot_id", "created_at"
    )

    settlement_attempts = (
        db.query(SettlementCaptureAttempt)
        .filter(SettlementCaptureAttempt.decision_snapshot_id.in_(decision_ids))
        .all()
        if decision_ids
        else []
    )
    latest_settlement_by_decision_id = _latest_by_key(
        settlement_attempts, "decision_snapshot_id", "created_at"
    )

    # Most recent real decision-stage scheduler_run_event per calendar
    # event, within a real recent window -- "eligibility, as last
    # actually evaluated by a real scheduler run", never a live re-check.
    recent_run_events = (
        db.query(SchedulerRunEvent)
        .filter(
            SchedulerRunEvent.earnings_calendar_event_id.in_(event_ids),
            SchedulerRunEvent.stage == "decision",
        )
        .all()
    )
    latest_run_event_by_calendar_id = _latest_by_key(
        recent_run_events, "earnings_calendar_event_id", "occurred_at"
    )

    # Same pattern, one stage earlier -- the most recent real
    # preparation-stage scheduler_run_event (services/earnings_research_
    # preparation.py), never a live re-check of readiness.
    recent_preparation_events = (
        db.query(SchedulerRunEvent)
        .filter(
            SchedulerRunEvent.earnings_calendar_event_id.in_(event_ids),
            SchedulerRunEvent.stage == "preparation",
        )
        .all()
    )
    latest_preparation_event_by_calendar_id = _latest_by_key(
        recent_preparation_events, "earnings_calendar_event_id", "occurred_at"
    )

    # Pre-live hardening (2026-08-25): the real, current source of truth
    # for "has research actually finished" is now the queue-managed
    # ResearchPreparationJob row itself (services/research_preparation_
    # queue.py), not a preparation-stage SchedulerRunEvent -- enqueueing
    # (which still records "queued"/"already_ready"/"filtered_out" events
    # above) finishes in milliseconds, well before the dedicated research-
    # worker actually does the real work, so those events alone can no
    # longer tell "prepared" from "still queued". "filtered_out" is still
    # read from the event above (a filtered candidate never gets a job
    # row at all); everything else falls back to this table instead.
    recent_preparation_jobs = (
        db.query(ResearchPreparationJob)
        .filter(ResearchPreparationJob.earnings_calendar_event_id.in_(event_ids))
        .all()
    )
    latest_preparation_job_by_calendar_id = _latest_by_key(
        recent_preparation_jobs, "earnings_calendar_event_id", "id"
    )

    results: list[PipelineEvent] = []
    for event in events:
        schedule = compute_entry_exit_schedule(
            event.earnings_date, _timing_to_announcement(event.earnings_time)
        )
        decision = decision_by_event_id.get(event.id)
        latest_entry = latest_entry_by_decision_id.get(decision.id) if decision else None
        latest_settlement = latest_settlement_by_decision_id.get(decision.id) if decision else None
        latest_run_event = latest_run_event_by_calendar_id.get(event.id)
        latest_preparation_event = latest_preparation_event_by_calendar_id.get(event.id)
        latest_preparation_job = latest_preparation_job_by_calendar_id.get(event.id)
        prep_outcome, prep_reason, prep_at = _effective_preparation_status(
            latest_preparation_job, latest_preparation_event
        )

        lifecycle = derive_lifecycle_state(
            schedule=schedule,
            now=now,
            latest_decision_outcome=latest_run_event.outcome if latest_run_event else None,
            latest_decision_reason=latest_run_event.reason if latest_run_event else None,
            decision_snapshot=decision,
            latest_entry_attempt=latest_entry,
            latest_settlement_attempt=latest_settlement,
            latest_preparation_outcome=prep_outcome,
            latest_preparation_reason=prep_reason,
        )
        timeline = _build_timeline(
            event=event,
            schedule=schedule,
            preparation_outcome=prep_outcome,
            preparation_reason=prep_reason,
            preparation_occurred_at=prep_at,
            latest_decision_run_event=latest_run_event,
            decision_snapshot=decision,
            latest_entry_attempt=latest_entry,
            latest_settlement_attempt=latest_settlement,
        )
        results.append(
            PipelineEvent(
                calendar_event_id=event.id,
                symbol=event.symbol,
                company_name=event.company_name,
                market_cap=str(event.market_cap) if event.market_cap is not None else None,
                earnings_date=event.earnings_date.isoformat(),
                earnings_timing=event.earnings_time.value,
                entry_timestamp=schedule.entry_timestamp,
                exit_timestamp=schedule.exit_timestamp,
                lifecycle_state=lifecycle.state,
                lifecycle_reason=lifecycle.reason,
                next_action=lifecycle.next_action,
                next_action_at=lifecycle.next_action_at,
                decision_snapshot_id=decision.id if decision else None,
                entry_capture_attempt_id=latest_entry.id if latest_entry else None,
                settlement_capture_attempt_id=latest_settlement.id if latest_settlement else None,
                timeline=timeline,
            )
        )
    return results


# --------------------------------------------------------------------------
# Section 6 -- Scheduler Job Monitor. Combines the existing live
# SchedulerStatus (registration + APScheduler's own next_run_time) with
# the new, persisted SchedulerRun history for duration/items/error --
# neither alone answers "what happened last time and how long did it
# take."
# --------------------------------------------------------------------------

ALL_JOB_IDS = (
    CALENDAR_SYNC_JOB_ID,
    EARNINGS_RESEARCH_PREPARATION_JOB_ID,
    DECISION_AND_ENTRY_CAPTURE_JOB_ID,
    EXIT_CAPTURE_JOB_ID,
    IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
)


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
    status_by_job_id = {j.job_id: j for j in scheduler_status.jobs}
    views: list[SchedulerJobView] = []
    for job_id in ALL_JOB_IDS:
        status = status_by_job_id.get(job_id)
        latest_run = _latest_scheduler_run(db, job_id)
        fallback_last_run_at = status.last_run_at if status else None
        fallback_last_run_status = status.last_run_status if status else None
        views.append(
            SchedulerJobView(
                job_id=job_id,
                enabled=status is not None,
                last_run_at=latest_run.started_at if latest_run else fallback_last_run_at,
                last_run_status=latest_run.status if latest_run else fallback_last_run_status,
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


# --------------------------------------------------------------------------
# Pre-live hardening (2026-08-25) -- live progress for the durable
# research-preparation queue (services/research_preparation_queue.py),
# not for a single scheduler "run": since run_earnings_research_
# preparation_job now only enqueues (returns in well under a second),
# there is no meaningful "run still in progress" to key off any more --
# the real, possibly-minutes-long work happens continuously, out of
# process, in the dedicated research-worker. Every value here is a real,
# already-persisted read: queue_depth from the same claimable-row count
# the worker itself uses to decide whether there's more work,
# completed/failed from real terminal ResearchPreparationJob rows the
# queue produced (earnings_calendar_event_id IS NOT NULL -- excludes the
# unrelated on-demand Search-page path), and the currently-claimed job
# (if any) for the live current company/stage/heartbeat/elapsed --
# never a live re-computation of readiness.
# --------------------------------------------------------------------------

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

    # By construction (Section 10: a single worker) at most one row is
    # ever RUNNING here; ordered defensively in case that ever changes.
    current_job = (
        db.query(ResearchPreparationJob)
        .filter(_QUEUE_MANAGED, ResearchPreparationJob.status == JobStatus.RUNNING)
        .order_by(ResearchPreparationJob.heartbeat_at.desc().nullslast())
        .first()
    )
    if current_job is None:
        return PreparationProgress(
            queue_depth=queue_depth,
            completed=completed,
            failed=failed,
            worker_active=False,
            current_symbol=None,
            current_stage=None,
            step_index=None,
            step_total=None,
            attempt=None,
            heartbeat_seconds_ago=None,
            elapsed_seconds=None,
        )

    current_stage = None
    step_index = None
    running_step = next((s for s in current_job.steps if s.get("status") == "running"), None)
    if running_step is not None:
        step = PreparationStep(running_step["step"])
        current_stage = STEP_LABELS.get(step)
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


# --------------------------------------------------------------------------
# Section 7 -- Today's Execution Summary. Derived entirely from the same
# get_todays_pipeline() result the pipeline view itself uses -- one real
# source of truth, never a second, separately-computed count that could
# silently disagree with what the page already shows row-by-row.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionSummary:
    todays_events: int
    eligibility_passed: int
    eligibility_failed: int
    decisions_created: int
    waiting_for_entry: int
    entries_captured: int
    entry_failures: int
    settlements_due: int
    settled: int
    settlement_failures: int


def compute_execution_summary(
    pipeline_events: list[PipelineEvent], *, now: datetime | None = None
) -> ExecutionSummary:
    now = now or datetime.now(UTC)
    today_et = now.astimezone(EASTERN).date()
    todays = [
        e
        for e in pipeline_events
        if e.entry_timestamp.astimezone(EASTERN).date() == today_et
        or e.exit_timestamp.astimezone(EASTERN).date() == today_et
    ]
    return ExecutionSummary(
        todays_events=len(todays),
        eligibility_passed=sum(
            1 for e in todays if e.lifecycle_state not in (STATE_NOT_ELIGIBLE, STATE_SKIPPED)
        ),
        eligibility_failed=sum(
            1 for e in todays if e.lifecycle_state in (STATE_NOT_ELIGIBLE, STATE_SKIPPED)
        ),
        decisions_created=sum(1 for e in todays if e.decision_snapshot_id is not None),
        waiting_for_entry=sum(1 for e in todays if e.lifecycle_state == STATE_WAITING_FOR_ENTRY),
        entries_captured=sum(
            1
            for e in todays
            if e.lifecycle_state
            in (STATE_WAITING_FOR_SETTLEMENT, STATE_SETTLED, STATE_SETTLEMENT_FAILED)
        ),
        entry_failures=sum(1 for e in todays if e.lifecycle_state == STATE_ENTRY_FAILED),
        settlements_due=sum(1 for e in todays if e.lifecycle_state == STATE_WAITING_FOR_SETTLEMENT),
        settled=sum(1 for e in todays if e.lifecycle_state == STATE_SETTLED),
        settlement_failures=sum(1 for e in todays if e.lifecycle_state == STATE_SETTLEMENT_FAILED),
    )


# --------------------------------------------------------------------------
# Post-official-run cleanup (2026-08-27), Section 3/4 -- Today's Official
# Run. Distinct from ExecutionSummary/compute_execution_summary above
# (kept unchanged, relabeled "Current Pipeline Summary" on the frontend):
# that view is deliberately wide -- every event whose own computed entry
# OR exit timestamp lands on today's NY date, drawn from a real but
# multi-day-wide EarningsCalendarEvent window (get_todays_pipeline's own
# window_start/window_end) -- useful for "what does the pipeline look
# like right now", but not an honest answer to "what did the scheduler
# actually do today." This view answers exactly that, and only that:
# sourced strictly from the real, persisted SchedulerRun/SchedulerRunEvent
# rows for TODAY's own decision_and_entry_capture/exit_capture runs, never
# inferred from the broader pipeline table.
# --------------------------------------------------------------------------

# stage="decision" outcomes that are real, successful evaluations but
# never a decision (ineligible, not yet due, too late, no researched
# company, or -- rare, idempotent re-run only -- already frozen). All
# fold into one "skipped/ineligible" bucket for the top-level
# reconciliation, matching decision_pipeline.py's own Outcome vocabulary.
_DECISION_NON_CREATED_OUTCOMES = (
    "skipped_ineligible",
    "skipped_not_due",
    "skipped_too_late",
    "skipped_no_company",
    "already_frozen",
)


@dataclass(frozen=True)
class TodaysOfficialRun:
    """``found=False`` is the honest answer before the scheduler has
    fired today at all (e.g. before 15:55 ET) -- never a fabricated
    all-zero summary pretending a run already happened.

    The reconciliation this project's own Section 2 cleanup asks for
    holds by construction, not by a runtime assertion (a read path must
    never raise over its own display data): ``evaluated`` (read straight
    from the real, persisted SchedulerRun.items_evaluated) equals
    ``skipped_ineligible + no_action + entries_captured + entries_failed
    + pipeline_failed`` -- every due event gets exactly one stage=
    "decision" SchedulerRunEvent, and every created decision gets exactly
    one stage="entry" one, in services/scheduler.py's own job loop.
    ``decisions_created`` is the natural roll-up of the latter three
    (no_action + entries_captured + entries_failed), shown separately
    because Operations displays it as its own real number.
    """

    found: bool
    run_started_at: datetime | None
    run_finished_at: datetime | None
    run_status: str | None
    evaluated: int
    skipped_ineligible: int
    #: V4 consolidation, Section 14 -- chain/contract lookups that failed at
    #: the provider. Counted separately so an IBKR symbol-format or transport
    #: problem can never masquerade as a business filter.
    contract_resolution_failed: int
    decisions_created: int
    no_action: int
    entries_captured: int
    entries_failed: int
    pipeline_failed: int
    settlements_captured: int
    settlements_failed: int


def _todays_scheduler_run(db: Session, job_id: str, today_et: date) -> SchedulerRun | None:
    run = _latest_scheduler_run(db, job_id)
    if run is None or run.started_at.astimezone(EASTERN).date() != today_et:
        return None
    return run


def get_todays_official_run(db: Session, *, now: datetime | None = None) -> TodaysOfficialRun:
    now = now or datetime.now(UTC)
    today_et = now.astimezone(EASTERN).date()

    decision_run = _todays_scheduler_run(db, DECISION_AND_ENTRY_CAPTURE_JOB_ID, today_et)
    settlement_run = _todays_scheduler_run(db, EXIT_CAPTURE_JOB_ID, today_et)

    if decision_run is None and settlement_run is None:
        return TodaysOfficialRun(
            found=False,
            run_started_at=None,
            run_finished_at=None,
            run_status=None,
            evaluated=0,
            skipped_ineligible=0,
            contract_resolution_failed=0,
            decisions_created=0,
            no_action=0,
            entries_captured=0,
            entries_failed=0,
            pipeline_failed=0,
            settlements_captured=0,
            settlements_failed=0,
        )

    skipped_ineligible = 0
    contract_resolution_failed = 0
    decisions_created = 0
    no_action = 0
    entries_captured = 0
    entries_failed = 0
    pipeline_failed = 0
    evaluated = 0

    if decision_run is not None:
        evaluated = decision_run.items_evaluated or 0
        for event in (
            db.query(SchedulerRunEvent)
            .filter(SchedulerRunEvent.scheduler_run_id == decision_run.id)
            .all()
        ):
            if event.stage == "decision":
                if event.outcome == "created":
                    decisions_created += 1
                elif event.outcome == "contract_resolution_failed":
                    contract_resolution_failed += 1
                elif event.outcome in _DECISION_NON_CREATED_OUTCOMES:
                    skipped_ineligible += 1
                else:  # "failed" -- a genuine generate_decision()/freeze exception
                    pipeline_failed += 1
            elif event.stage == "entry":
                if event.outcome == OUTCOME_ENTRY_CAPTURED:
                    entries_captured += 1
                elif event.outcome == OUTCOME_DECISION_NO_ACTION:
                    no_action += 1
                elif event.outcome == OUTCOME_ENTRY_FAILED:
                    entries_failed += 1

    settlements_captured = 0
    settlements_failed = 0
    if settlement_run is not None:
        for event in (
            db.query(SchedulerRunEvent)
            .filter(SchedulerRunEvent.scheduler_run_id == settlement_run.id)
            .all()
        ):
            if event.outcome == OUTCOME_SETTLEMENT_CAPTURED:
                settlements_captured += 1
            elif event.outcome == OUTCOME_SETTLEMENT_FAILED:
                settlements_failed += 1

    # At least one is real here -- the early return above already
    # covers "both None".
    run_for_timing = decision_run if decision_run is not None else settlement_run
    assert run_for_timing is not None
    return TodaysOfficialRun(
        found=True,
        run_started_at=run_for_timing.started_at,
        run_finished_at=run_for_timing.finished_at,
        run_status=run_for_timing.status,
        evaluated=evaluated,
        skipped_ineligible=skipped_ineligible,
        contract_resolution_failed=contract_resolution_failed,
        decisions_created=decisions_created,
        no_action=no_action,
        entries_captured=entries_captured,
        entries_failed=entries_failed,
        pipeline_failed=pipeline_failed,
        settlements_captured=settlements_captured,
        settlements_failed=settlements_failed,
    )


# --------------------------------------------------------------------------
# Section 8 -- Failure Center. Real, actionable problems only, pulled
# from real tables: provider_health_event (non-CONNECTED events),
# scheduler_run/scheduler_run_event (job- and item-level failures),
# entry/settlement CaptureStatus.FAILED rows.
# --------------------------------------------------------------------------

FAILURE_LOOKBACK = timedelta(days=3)


@dataclass(frozen=True)
class FailureEntry:
    occurred_at: datetime
    symbol: str | None
    stage: str
    category: str
    explanation: str
    detail: str | None
    retryability: str  # "RETRYABLE" | "NOT_RETRYABLE" | "WINDOW_MISSED"


def _entry_retryability(reason: str | None) -> str:
    if reason and "no-lookahead" in reason.lower():
        return "WINDOW_MISSED"
    if reason and "past the safe window" in reason.lower():
        return "WINDOW_MISSED"
    return "RETRYABLE"


def get_recent_failures(db: Session, *, now: datetime | None = None) -> list[FailureEntry]:
    now = now or datetime.now(UTC)
    since = now - FAILURE_LOOKBACK
    failures: list[FailureEntry] = []

    for health_event in (
        db.query(ProviderHealthEvent)
        .filter(
            ProviderHealthEvent.occurred_at >= since,
            ProviderHealthEvent.status != "CONNECTED",
        )
        .order_by(ProviderHealthEvent.occurred_at.desc())
        .all()
    ):
        failures.append(
            FailureEntry(
                occurred_at=health_event.occurred_at,
                symbol=None,
                stage=health_event.domain,
                category=f"{health_event.provider} {health_event.status.value}",
                explanation=f"{health_event.provider} ({health_event.domain}) reported "
                f"{health_event.status.value}",
                detail=health_event.detail,
                retryability="RETRYABLE",
            )
        )

    for run_event in (
        db.query(SchedulerRunEvent)
        .filter(
            SchedulerRunEvent.occurred_at >= since,
            # Post-official-run cleanup (2026-08-27), Section 1 -- three
            # distinct real-failure outcomes across the three stages
            # (decision/entry/settlement), not one shared "failed"
            # literal -- see scheduler_run_tracking.py::FAILURE_OUTCOMES.
            SchedulerRunEvent.outcome.in_(FAILURE_OUTCOMES),
        )
        .order_by(SchedulerRunEvent.occurred_at.desc())
        .all()
    ):
        failures.append(
            FailureEntry(
                occurred_at=run_event.occurred_at,
                symbol=run_event.symbol,
                stage=run_event.stage,
                category=f"{run_event.stage} failed",
                explanation=f"{run_event.stage.capitalize()} failed for {run_event.symbol}",
                detail=run_event.reason,
                retryability=_entry_retryability(run_event.reason),
            )
        )

    for run in (
        db.query(SchedulerRun)
        .filter(SchedulerRun.started_at >= since, SchedulerRun.status == "error")
        .order_by(SchedulerRun.started_at.desc())
        .all()
    ):
        failures.append(
            FailureEntry(
                occurred_at=run.started_at,
                symbol=None,
                stage=run.job_id,
                category="scheduler job failed",
                explanation=f"Scheduler job {run.job_id} failed to complete",
                detail=run.error_summary,
                retryability="RETRYABLE",
            )
        )

    failures.sort(key=lambda f: f.occurred_at, reverse=True)
    return failures


# --------------------------------------------------------------------------
# Pre-live hardening (2026-08-25) Section 7 -- missed-critical-job
# detection. Pure function, no DB/network access of its own: every input
# is already-fetched real state (SchedulerJobView from get_scheduler_
# jobs(), PipelineEvent from get_todays_pipeline(), SystemHealth from
# get_system_health()) -- this only cross-references what's already on
# the page, it never makes this page slower or more expensive to load.
# Observability only, exactly like the rest of this module: no retry, no
# force-run, nothing here ever writes anything.
# --------------------------------------------------------------------------

# How far past a job's own next_run_time (APScheduler's real, live value
# -- see get_scheduler_status()) it's allowed to drift before this is
# treated as "the scheduler appears to have stalled on this job," not a
# normal few-second scheduling jitter. Deliberately job-agnostic (works
# for both the 15:55 ET cron jobs and the 10-minute interval healthcheck
# job): a cron trigger's next_run_time stays frozen at its own scheduled
# fire time until the scheduler's internal loop actually processes it,
# regardless of that job's own success/failure -- confirmed live this
# session -- so "next_run_time is now more than this far in the past" is
# a real, honest signal the scheduler hasn't even attempted it yet.
MISSED_JOB_GRACE = timedelta(minutes=5)

# How long a SchedulerRun is allowed to sit with started_at set and
# finished_at still null before it's flagged as possibly stuck -- well
# beyond any legitimate real run (TestMultiCompanyThroughput's own
# measurement puts 5 real companies' worth of sequential orchestration
# well under a minute of real work), short enough that a genuinely wedged
# job is still noticed with real time left in the legal execution window.
STUCK_RUN_THRESHOLD = timedelta(minutes=15)

# How close to a pipeline event's own next_action_at an IBKR outage is
# still worth a dedicated CRITICAL callout, distinct from the generic
# "IBKR is red" health card -- this is specifically "and something is
# due soon, so this outage is not merely cosmetic right now."
IBKR_OUTAGE_LOOKAHEAD = timedelta(minutes=30)

_UNPROCESSED_LIFECYCLE_STATES = (
    STATE_CALENDAR_DISCOVERED,
    STATE_READY_FOR_DECISION,
    STATE_WAITING_FOR_DECISION,
)


def detect_missed_job_alerts(
    scheduler_jobs: list[SchedulerJobView],
    pipeline_events: list[PipelineEvent],
    health: SystemHealth,
    *,
    now: datetime | None = None,
    forward_test_activation_at: datetime | None = None,
) -> list[FailureEntry]:
    """Never infers success from absence of an error: each check below
    is a positive, concrete condition (a job visibly overdue, a run
    visibly still open past a generous threshold, a due event with no
    trace of processing, IBKR visibly down with something due soon) --
    never "no news is good news." See the module-level constants above
    for why each threshold is what it is.

    ``forward_test_activation_at`` (Settings.forward_test_activation_at,
    resolved by the caller -- this function stays a pure computation
    over already-resolved values, like the rest of this module):
    real events due before this timestamp never produce an
    unprocessed_due_event alert. Observability only -- see that
    setting's own docstring."""
    now = now or datetime.now(UTC)
    alerts: list[FailureEntry] = []

    for job in scheduler_jobs:
        if not job.enabled or job.next_run_time is None:
            continue
        overdue_by = now - job.next_run_time
        if overdue_by > MISSED_JOB_GRACE:
            alerts.append(
                FailureEntry(
                    occurred_at=job.next_run_time,
                    symbol=None,
                    stage=job.job_id,
                    category="missed_job",
                    explanation=(
                        f"Scheduler job {job.job_id} was due at "
                        f"{job.next_run_time.isoformat()} but has not started -- "
                        f"{int(overdue_by.total_seconds() // 60)} minutes overdue"
                    ),
                    detail=None,
                    retryability="RETRYABLE",
                )
            )

    for job in scheduler_jobs:
        if job.last_run_status != "running":
            continue
        # A SchedulerJobView's own last_run_at is the real started_at of
        # the most recent SchedulerRun row when one exists (see get_
        # scheduler_jobs()) -- "running" only appears here for a row
        # whose finished_at is still null.
        if job.last_run_at is not None and now - job.last_run_at > STUCK_RUN_THRESHOLD:
            alerts.append(
                FailureEntry(
                    occurred_at=job.last_run_at,
                    symbol=None,
                    stage=job.job_id,
                    category="job_running_too_long",
                    explanation=(
                        f"Scheduler job {job.job_id} started at "
                        f"{job.last_run_at.isoformat()} and has not finished after "
                        f"{int((now - job.last_run_at).total_seconds() // 60)} minutes"
                    ),
                    detail=None,
                    retryability="NOT_RETRYABLE",
                )
            )

    for event in pipeline_events:
        if (
            event.next_action_at is not None
            and event.next_action_at < now - MISSED_JOB_GRACE
            and event.lifecycle_state in _UNPROCESSED_LIFECYCLE_STATES
            and (
                forward_test_activation_at is None
                or event.next_action_at >= forward_test_activation_at
            )
        ):
            alerts.append(
                FailureEntry(
                    occurred_at=event.next_action_at,
                    symbol=event.symbol,
                    stage="decision",
                    category="unprocessed_due_event",
                    explanation=(
                        f"{event.symbol} was due for {event.next_action or 'processing'} at "
                        f"{event.next_action_at.isoformat()} but shows no decision/entry "
                        "activity yet"
                    ),
                    detail=event.lifecycle_reason,
                    retryability="RETRYABLE",
                )
            )

    if health.ibkr.state != "green":
        soon_due = [
            e
            for e in pipeline_events
            if e.next_action_at is not None
            and now <= e.next_action_at <= now + IBKR_OUTAGE_LOOKAHEAD
        ]
        if soon_due:
            symbols = ", ".join(sorted({e.symbol for e in soon_due}))
            alerts.append(
                FailureEntry(
                    occurred_at=now,
                    symbol=None,
                    stage="ibkr",
                    category="ibkr_unavailable_before_entry",
                    explanation=(
                        f"IBKR is not connected and {len(soon_due)} event(s) are due within "
                        f"{int(IBKR_OUTAGE_LOOKAHEAD.total_seconds() // 60)} minutes: {symbols}"
                    ),
                    detail=health.ibkr.last_error,
                    retryability="RETRYABLE",
                )
            )

    alerts.sort(key=lambda f: f.occurred_at, reverse=True)
    return alerts


# --------------------------------------------------------------------------
# Section 10 -- Today's Pre-Flight Check. Real, existing checks composed
# together -- no new live probe beyond what get_system_health() already
# makes.
# --------------------------------------------------------------------------


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


def get_preflight_readiness(db: Session, health: SystemHealth) -> PreflightReadiness:
    portfolio = db.query(BenchmarkPortfolio).filter_by(is_active=True).first()
    checks = [
        PreflightCheck("IBKR authenticated", health.ibkr.authenticated),
    ]
    # IBKR TWS Migration, Phase 3 readiness (Section 26/27) -- a real gap
    # this task's own live validation surfaced: TWS never sets
    # live_account (see this function's own IbkrHealth.provider check
    # below and services/operations.py's get_system_health, TWS branch --
    # the TWS socket API has no equivalent of the Web Gateway's real
    # /iserver/accounts isPaper boolean this codebase has wired up). Under
    # the OLD, Web-only version of this check, that permanently reads as
    # `None is True` == False -- meaning a genuinely healthy, genuinely
    # LIVE TWS deployment (confirmed live, 2026-09-01: port 4001, real
    # account data) would show NOT READY forever, purely from an old
    # Web-specific assumption this transport structurally can't satisfy
    # the same way. Omitted entirely for TWS rather than forced to a
    # fabricated pass -- never claims a verification that didn't happen.
    if health.ibkr.provider != "tws":
        checks.append(
            PreflightCheck(
                "Live account confirmed",
                health.ibkr.live_account is True,
                None if health.ibkr.live_account is True else "paper account or unknown",
            )
        )
    checks += [
        PreflightCheck(
            "Market data available",
            health.ibkr.state in (_HEALTHY, _DEGRADED) and health.ibkr.connected,
        ),
        PreflightCheck(
            "Earnings calendar synced",
            health.earnings_calendar.last_successful_sync_at is not None,
        ),
        PreflightCheck("AI provider configured", health.ai_provider.configured),
        PreflightCheck("Scheduler running", health.scheduler.running),
        PreflightCheck(
            "Benchmark portfolio active",
            portfolio is not None,
            None if portfolio is not None else "no active benchmark_portfolio row",
        ),
        PreflightCheck("Database healthy", health.database.database_healthy),
    ]
    blockers = [c.label + (f" ({c.detail})" if c.detail else "") for c in checks if not c.passed]
    return PreflightReadiness(checks=checks, ready=len(blockers) == 0, blockers=blockers)


# --------------------------------------------------------------------------
# Section 11 -- Market Clock. Backend is authoritative for every
# timestamp shown (Section 11's own explicit requirement) -- the
# frontend never computes market-session or trading-calendar logic
# itself, only renders what this returns.
# --------------------------------------------------------------------------

ZURICH = ZoneInfo("Europe/Zurich")


@dataclass(frozen=True)
class MarketClock:
    utc_now: datetime
    new_york_now: datetime
    zurich_now: datetime
    market_session: str
    next_automatic_action_job_id: str | None
    next_automatic_action_at: datetime | None


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
