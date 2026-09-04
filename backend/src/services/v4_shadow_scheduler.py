"""V4 forward-test scheduler jobs (internal module name kept as v4_shadow).

Deliberately a SEPARATE module from services/scheduler.py: that module is
registration only; every V4 job body lives here.

TIMING (V4-only reset, 2026-09-02). The V4 cohort observes under its OWN
versioned clock, ``V4_ACTIVE_TIMING_POLICY`` (v2: decision/entry 15:30 ET,
settlement 15:30 ET on the first post-earnings trading day). Both windows
are derived from ``compute_entry_exit_schedule`` with that policy; the
grace/tolerance constants live in analytics/forward_windows.py.

THE 15:30 FORWARD WINDOW IS ONE JOB (v4.0.0 settlement-priority hardening).
Decision and settlement used to be two APScheduler jobs on the same cron;
they ran in two threads with no defined order, and the decision job held the
market-data lock around its ENTIRE run -- DeepSeek DecisionView generation
(~80 s per event) included -- so a due settlement could wait minutes for
the lock and then be stamped with the job-start time. Now ``v4_forward_window``
is the only 15:30 job and its order is structural:

    15:30 ET
      1. settle every position whose legal settlement window is open
         (settlement evidence is expiring; the lock is taken per position,
         only around quote acquisition, and the window is re-checked at the
         moment market data is actually acquired)
      2. begin new decision observations, each of which first settles
         anything that became due meanwhile and then holds the lock ONLY
         around its own TWS chain sweep -- never around DeepSeek, valuation,
         ranking or persistence.

Each phase records its own ``scheduler_run`` under the historical job ids
(``v4_shadow_settlement``, ``v4_shadow_decision``) so Operations history,
staleness and missed-run detection keep working unchanged.

DEADLINE GUARD. The decision phase stops STARTING new full evaluations at
DECISION_DEADLINE_ET (15:50) and records DEADLINE_SKIPPED evidence for the
due, research-ready events it could not start.

TELEMETRY. Every settlement attempt writes a ``v4_forward_window_telemetry``
row (scheduled / started / due detected / market data requested / acquired /
first contract request / required side ready / completed, lock wait, total),
and the decision phase writes one summary row with its total lock wait.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from core.config import get_settings

# Section 9 (V4 consolidation, 2026-09-02): scheduler-owned work uses the
# DEDICATED scheduler pool, exactly as the official V3 jobs in
# services/scheduler.py do. db/session.py explains the real,
# empirically-observed failure the two-pool split prevents: Operations
# Monitor polling alone was enough to exhaust a shared pool and starve a
# scheduled job of a connection for several cycles. Importing the
# API-facing SessionLocal here would have re-created that hazard for the
# shadow cohort -- and on the API side, where a stalled shadow job would
# have competed with user requests.
from db.session import SchedulerSessionLocal as SessionLocal

if TYPE_CHECKING:
    from providers.base import OptionsDataProvider

from services.scheduler_run_tracking import (
    RUN_STATUS_ERROR,
    RUN_STATUS_SKIPPED,
    RUN_STATUS_SUCCESS,
    finish_scheduler_run,
    start_scheduler_run,
)

log = logging.getLogger("services.v4_shadow_scheduler")

# Stable job ids -- chosen once and never renamed, so persisted scheduler
# history stays meaningful. Deliberately distinct from the official V3
# ids so they can never overload its success/failure counters.
#: The only 15:30 ET job. The two ids below are the PHASES it records.
V4_FORWARD_WINDOW_JOB_ID = "v4_forward_window"
V4_SHADOW_DECISION_JOB_ID = "v4_shadow_decision"
V4_SHADOW_SETTLEMENT_JOB_ID = "v4_shadow_settlement"
#: Retired 15:30 registrations that must never fire again (removed from the
#: persistent job store by migration b7d9f1a3c5e7 and, defensively, at startup).
RETIRED_V4_JOB_IDS = (V4_SHADOW_DECISION_JOB_ID, V4_SHADOW_SETTLEMENT_JOB_ID)

# Settlement window states. A pending configuration is settled ONLY inside
# the legal exit window; before it the job waits, after it the position is
# closed out as a terminal failure (never with a late, dishonest quote).
SETTLEMENT_NOT_DUE = "NOT_DUE"
SETTLEMENT_DUE = "DUE"
SETTLEMENT_WINDOW_MISSED = "WINDOW_MISSED"

#: Serialises the market-data sections of the two 15:30 ET jobs (see the
#: module docstring). In-process only: both jobs run in this scheduler.
V4_MARKET_DATA_LOCK = threading.Lock()


def v4_schedule_for_event(event):
    """The V4 cohort's legal schedule for one calendar event under the
    ACTIVE policy (v2: entry 15:30 ET, exit 15:30 ET on the first
    post-earnings trading day). Historical rows keep their own stored
    policy version; the window a settlement is OBSERVED in is always the
    active one (prospective transition)."""
    from analytics.decision_timing_policy import V4_ACTIVE_TIMING_POLICY  # noqa: PLC0415
    from analytics.earnings_timing import compute_entry_exit_schedule  # noqa: PLC0415
    from analytics.forward_windows import announcement_session  # noqa: PLC0415

    return compute_entry_exit_schedule(
        event.earnings_date, announcement_session(event), policy=V4_ACTIVE_TIMING_POLICY
    )


def due_for_v4_decision_now(event, now: datetime) -> bool:
    """True only inside the V4 decision window: [15:30 ET, 15:30 +
    LATE_CUTOFF_GRACE] on the legal pre-earnings trading day."""
    from analytics.forward_windows import LATE_CUTOFF_GRACE  # noqa: PLC0415

    schedule = v4_schedule_for_event(event)
    return schedule.entry_timestamp <= now <= schedule.entry_timestamp + LATE_CUTOFF_GRACE


def v4_settlement_window_state(event, now: datetime) -> str:
    """Where ``now`` sits relative to the legal exit window: exit time
    (15:30 ET, first post-earnings trading day) minus EARLY_CAPTURE_
    TOLERANCE, plus LATE_CUTOFF_GRACE. A same-day AMC settlement is
    impossible by construction (the exit date is the next trading day)."""
    from analytics.forward_windows import (  # noqa: PLC0415
        EARLY_CAPTURE_TOLERANCE,
        LATE_CUTOFF_GRACE,
    )

    schedule = v4_schedule_for_event(event)
    if now < schedule.exit_timestamp - EARLY_CAPTURE_TOLERANCE:
        return SETTLEMENT_NOT_DUE
    if now > schedule.exit_timestamp + LATE_CUTOFF_GRACE:
        return SETTLEMENT_WINDOW_MISSED
    return SETTLEMENT_DUE


def _due_candidate_events(db, now: datetime) -> list:
    """Day-level pre-filter of calendar events; the time-of-day window is
    then ``due_for_v4_decision_now``."""

    from analytics.forward_windows import DECISION_CANDIDATE_LOOKAHEAD_DAYS  # noqa: PLC0415
    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415

    horizon = now.date() + timedelta(days=DECISION_CANDIDATE_LOOKAHEAD_DAYS)
    return (
        db.query(EarningsCalendarEvent)
        .filter(EarningsCalendarEvent.earnings_date <= horizon)
        .filter(EarningsCalendarEvent.earnings_date >= now.date() - timedelta(days=2))
        .all()
    )


@dataclass
class SettlementTelemetry:
    """Timing evidence for ONE settlement attempt (Section 9). Every field is
    a real clock reading taken by the coordinator; nothing is estimated."""

    phase: str = "settlement"
    shadow_decision_id: int | None = None
    symbol: str | None = None
    scheduled_at: datetime | None = None
    job_started_at: datetime | None = None
    due_detected_at: datetime | None = None
    market_data_requested_at: datetime | None = None
    market_data_acquired_at: datetime | None = None
    first_contract_request_at: datetime | None = None
    required_side_ready_at: datetime | None = None
    completed_at: datetime | None = None
    lock_wait_ms: int | None = None
    total_ms: int | None = None
    outcome: str = "pending"
    detail: str | None = None


def _ms(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


class _TimedQuoteProvider:
    """Delegates to the real provider and records when the first contract was
    requested and when required-side quotes were back. Adds no quote, changes
    no quote: it only observes the call boundary."""

    def __init__(
        self, provider: Any, clock: Callable[[], datetime], telemetry: SettlementTelemetry
    ):
        self._provider = provider
        self._clock = clock
        self._telemetry = telemetry

    def get_quotes_for_known_contracts(self, *args: Any, **kwargs: Any) -> Any:
        if self._telemetry.first_contract_request_at is None:
            self._telemetry.first_contract_request_at = self._clock()
        result = self._provider.get_quotes_for_known_contracts(*args, **kwargs)
        self._telemetry.required_side_ready_at = self._clock()
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


@dataclass
class SettlementRunSummary:
    """What one settlement pass did. ``not_due`` decisions were left
    untouched for a later run; ``evaluated`` counts only the decisions
    that were inside or past their legal window."""

    evaluated: int = 0
    settled: int = 0
    failed: int = 0
    not_due: int = 0
    window_missed: int = 0
    lock_wait_ms_max: int = 0

    def absorb(self, other: SettlementRunSummary) -> None:
        self.evaluated += other.evaluated
        self.settled += other.settled
        self.failed += other.failed
        self.not_due += other.not_due
        self.window_missed += other.window_missed
        self.lock_wait_ms_max = max(self.lock_wait_ms_max, other.lock_wait_ms_max)


def settle_due_cohorts(
    db,
    *,
    provider,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    on_telemetry: Callable[[SettlementTelemetry], None] | None = None,
    scheduled_at: datetime | None = None,
    job_started_at: datetime | None = None,
    market_data_lock=None,
) -> SettlementRunSummary:
    """Six-cohort settlement: every decision with at least one OBSERVED
    configuration entry that has no configuration settlement yet, gated by
    the legal exit window of its OWN calendar event.

    NOT_DUE   -> left pending (a 15:30 entry is never settled the same
                 afternoon; T+1 15:30 ET is the earliest honest exit).
    DUE       -> the cohort service dedupes the unique contracts across
                 all held candidates and issues ONE quote call per
                 expiration group -- never one per configuration.
    MISSED    -> each pending configuration is closed as a terminal
                 SETTLEMENT_WINDOW_MISSED failure. No later quote is ever
                 used as exit evidence.

    The market-data lock is taken PER position and only around quote
    acquisition + persistence of that position. The window is re-checked
    with the real clock at the moment the lock is acquired: a position that
    was due when detected but whose window closed while waiting is closed
    as WINDOW_MISSED -- it is never quoted late and stamped early. The
    settlement instant recorded on the rows is the acquisition instant.

    ``clock`` defaults to a fixed ``now`` (deterministic tests); the
    scheduler job passes the wall clock.
    """
    from analytics.decision_timing_policy import V4_ACTIVE_TIMING_POLICY  # noqa: PLC0415
    from analytics.forward_windows import LATE_CUTOFF_GRACE  # noqa: PLC0415
    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415
    from models.v4_shadow import (  # noqa: PLC0415
        V4ShadowConfigEntry,
        V4ShadowConfigSettlement,
        V4ShadowDecision,
    )
    from services.v4_shadow_cohort import (  # noqa: PLC0415
        fail_missed_settlement_window,
        settle_shadow_decision_cohorts,
    )

    clock = clock or (lambda: now)
    lock = market_data_lock if market_data_lock is not None else V4_MARKET_DATA_LOCK
    policy_version = V4_ACTIVE_TIMING_POLICY.version

    summary = SettlementRunSummary()
    settled_config_ids = db.query(V4ShadowConfigSettlement.shadow_config_result_id)
    pending_decision_ids = {
        e.shadow_decision_id
        for e in db.query(V4ShadowConfigEntry)
        .filter(V4ShadowConfigEntry.status == "OBSERVED")
        .filter(~V4ShadowConfigEntry.shadow_config_result_id.in_(settled_config_ids))
    }
    pending = (
        db.query(V4ShadowDecision)
        .filter(V4ShadowDecision.id.in_(pending_decision_ids or [0]))
        .order_by(V4ShadowDecision.id)
        .all()
    )
    events = {
        e.id: e
        for e in db.query(EarningsCalendarEvent).filter(
            EarningsCalendarEvent.id.in_([d.earnings_calendar_event_id for d in pending] or [0])
        )
    }

    def _missed_detail(event, at: datetime) -> str:
        schedule = v4_schedule_for_event(event)
        return (
            f"legal settlement window {schedule.exit_timestamp.isoformat()} "
            f"(+{LATE_CUTOFF_GRACE}) had already passed at {at.isoformat()} -- "
            "no honest live exit observation is possible"
        )

    for decision in pending:
        telemetry = SettlementTelemetry(
            shadow_decision_id=decision.id,
            symbol=decision.ticker,
            scheduled_at=scheduled_at,
            job_started_at=job_started_at,
        )
        try:
            detected_at = clock()
            event = events.get(decision.earnings_calendar_event_id)
            if event is None:
                state = SETTLEMENT_WINDOW_MISSED
                detail = (
                    f"calendar event {decision.earnings_calendar_event_id} no longer exists; "
                    "the legal exit window cannot be established"
                )
            else:
                state = v4_settlement_window_state(event, detected_at)
                detail = _missed_detail(event, detected_at)

            if state == SETTLEMENT_NOT_DUE:
                summary.not_due += 1
                continue

            summary.evaluated += 1
            telemetry.due_detected_at = detected_at
            if state == SETTLEMENT_DUE:
                telemetry.market_data_requested_at = clock()
                with lock:
                    acquired_at = clock()
                    telemetry.market_data_acquired_at = acquired_at
                    telemetry.lock_wait_ms = _ms(telemetry.market_data_requested_at, acquired_at)
                    summary.lock_wait_ms_max = max(
                        summary.lock_wait_ms_max, telemetry.lock_wait_ms or 0
                    )
                    # Re-check with the clock that will be stamped on the rows:
                    # the window may have closed while waiting for the lock.
                    if (
                        event is not None
                        and v4_settlement_window_state(event, acquired_at)
                        == SETTLEMENT_WINDOW_MISSED
                    ):
                        missed = fail_missed_settlement_window(
                            db,
                            decision=decision,
                            observed_at=acquired_at,
                            detail=_missed_detail(event, acquired_at),
                            timing_policy_version=policy_version,
                        )
                        summary.failed += missed
                        summary.window_missed += 1
                        telemetry.outcome = "window_missed"
                        telemetry.detail = "window closed while waiting for market data"
                    else:
                        result = settle_shadow_decision_cohorts(
                            db,
                            provider=cast(
                                "OptionsDataProvider",
                                _TimedQuoteProvider(provider, clock, telemetry),
                            ),
                            decision=decision,
                            observed_at=acquired_at,
                            timing_policy_version=policy_version,
                        )
                        summary.settled += result.settled
                        summary.failed += result.failed
                        telemetry.outcome = (
                            "settled"
                            if result.failed == 0
                            else ("partially_failed" if result.settled else "failed")
                        )
                        telemetry.detail = (
                            f"settled={result.settled} failed={result.failed} "
                            f"quote_calls={result.quote_calls}"
                        )
            else:
                summary.failed += fail_missed_settlement_window(
                    db,
                    decision=decision,
                    observed_at=detected_at,
                    detail=detail,
                    timing_policy_version=policy_version,
                )
                summary.window_missed += 1
                telemetry.outcome = "window_missed"
                telemetry.detail = detail
        except Exception as exc:  # noqa: BLE001 -- one decision must not stop the run
            summary.failed += 1
            telemetry.outcome = "error"
            telemetry.detail = f"{type(exc).__name__}: {exc}"
            log.error("v4 settlement failed for decision %s", decision.id, exc_info=True)
        finally:
            if telemetry.due_detected_at is not None:
                telemetry.completed_at = clock()
                telemetry.total_ms = _ms(
                    telemetry.job_started_at or telemetry.due_detected_at, telemetry.completed_at
                )
                if on_telemetry is not None:
                    on_telemetry(telemetry)
    return summary


@dataclass
class ForwardWindowSummary:
    """One 15:30 forward window: settlements first, then decisions."""

    settlement: SettlementRunSummary
    decisions: Any = None  # ShadowRunSummary | None (None when the phase was skipped)
    telemetry: list[SettlementTelemetry] = field(default_factory=list)
    #: Settlements performed by the pre-evaluation hook DURING the decision
    #: phase (a position that became due after phase 1) -- always 0 unless the
    #: window opened mid-run.
    settled_during_decisions: int = 0


def window_instant_for(now: datetime) -> datetime:
    """The scheduled 15:30 ET instant of ``now``'s Eastern trading day."""
    from analytics.decision_timing_policy import V4_ACTIVE_TIMING_POLICY  # noqa: PLC0415
    from analytics.earnings_timing import EASTERN  # noqa: PLC0415

    local = now.astimezone(EASTERN)
    return datetime.combine(local.date(), V4_ACTIVE_TIMING_POLICY.entry_time, tzinfo=EASTERN)


def run_forward_window(
    db,
    settings,
    *,
    provider,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    view_generator=None,
    candidate_events=None,
    deadline: datetime | None = None,
    scheduled_at: datetime | None = None,
    job_started_at: datetime | None = None,
    before_phase: Callable[[str], None] | None = None,
    after_phase: Callable[[str, Any], None] | None = None,
    on_telemetry: Callable[[SettlementTelemetry], None] | None = None,
) -> ForwardWindowSummary:
    """The forward-window coordinator (pure orchestration over ``db``).

    Phase ``settlement``: every due position, in decision order, each holding
    the market-data lock only for its own quote acquisition.
    Phase ``decision``: the due, research-ready events; before EACH full
    evaluation the coordinator settles anything that became due meanwhile,
    and the orchestration holds the lock only around its TWS chain sweep.
    A due settlement is therefore never queued behind a new decision, and a
    slow DecisionView never holds the lock at all.
    """
    from services.v4_shadow_orchestration import (  # noqa: PLC0415
        default_view_generator,
        run_shadow_decisions_for_due_events,
    )

    clock = clock or (lambda: now)
    telemetry: list[SettlementTelemetry] = []

    def _sink(t: SettlementTelemetry) -> None:
        telemetry.append(t)
        if on_telemetry is not None:
            on_telemetry(t)

    if before_phase is not None:
        before_phase("settlement")
    settlement = settle_due_cohorts(
        db,
        provider=provider,
        now=now,
        clock=clock,
        on_telemetry=_sink,
        scheduled_at=scheduled_at,
        job_started_at=job_started_at,
    )
    if after_phase is not None:
        after_phase("settlement", settlement)

    summary = ForwardWindowSummary(settlement=settlement, telemetry=telemetry)

    def _settle_newly_due() -> None:
        # Cheap when nothing is due (one indexed query); guarantees a position
        # whose window opened after phase 1 is still settled before any new
        # decision work starts.
        late = settle_due_cohorts(
            db,
            provider=provider,
            now=clock(),
            clock=clock,
            on_telemetry=_sink,
            scheduled_at=scheduled_at,
            job_started_at=job_started_at,
        )
        summary.settled_during_decisions += late.settled
        settlement.absorb(
            SettlementRunSummary(
                evaluated=late.evaluated,
                settled=late.settled,
                failed=late.failed,
                window_missed=late.window_missed,
                lock_wait_ms_max=late.lock_wait_ms_max,
            )
        )

    if before_phase is not None:
        before_phase("decision")
    decisions = run_shadow_decisions_for_due_events(
        db,
        settings,
        now=now,
        provider=provider,
        view_generator=view_generator or default_view_generator,
        due_predicate=due_for_v4_decision_now,
        candidate_events=(
            candidate_events if candidate_events is not None else _due_candidate_events(db, now)
        ),
        deadline=deadline,
        clock=clock,
        market_data_lock=V4_MARKET_DATA_LOCK,
        before_evaluation=_settle_newly_due,
    )
    summary.decisions = decisions
    if after_phase is not None:
        after_phase("decision", decisions)
    return summary


def _persist_telemetry(db, telemetry: SettlementTelemetry, scheduler_run_id: int | None) -> None:
    from models.v4_shadow import V4ForwardWindowTelemetry  # noqa: PLC0415

    db.add(
        V4ForwardWindowTelemetry(
            phase=telemetry.phase,
            scheduler_run_id=scheduler_run_id,
            shadow_decision_id=telemetry.shadow_decision_id,
            symbol=telemetry.symbol,
            scheduled_at=telemetry.scheduled_at,
            job_started_at=telemetry.job_started_at,
            due_detected_at=telemetry.due_detected_at,
            market_data_requested_at=telemetry.market_data_requested_at,
            market_data_acquired_at=telemetry.market_data_acquired_at,
            first_contract_request_at=telemetry.first_contract_request_at,
            required_side_ready_at=telemetry.required_side_ready_at,
            completed_at=telemetry.completed_at,
            lock_wait_ms=telemetry.lock_wait_ms,
            total_ms=telemetry.total_ms,
            outcome=telemetry.outcome,
            detail=telemetry.detail,
        )
    )


def run_v4_forward_window_job(
    *, now: datetime | None = None, clock: Callable[[], datetime] | None = None
) -> None:
    """The 15:30 ET job: settle due positions, then observe new decisions.

    Records one ``scheduler_run`` per phase under the historical phase ids.
    Never raises: every failure is recorded as run/telemetry evidence.
    ``clock`` is the wall clock (tests inject a controlled one); ``now`` is the
    legal window instant and defaults to the clock.
    """
    clock = clock or (lambda: datetime.now(UTC))
    db = SessionLocal()
    runs: dict[str, Any] = {}
    try:
        settings = get_settings()
        if not settings.v4_shadow_enabled:
            # Defence in depth: the job is not registered while disabled,
            # but must still refuse to act if it somehow is.
            for job_id in (V4_SHADOW_SETTLEMENT_JOB_ID, V4_SHADOW_DECISION_JOB_ID):
                finish_scheduler_run(db, start_scheduler_run(db, job_id), status=RUN_STATUS_SKIPPED)
            return

        from analytics.forward_windows import decision_deadline_for  # noqa: PLC0415
        from providers.factory import get_options_provider  # noqa: PLC0415

        job_started_at = clock()
        resolved_now = now or job_started_at
        scheduled_at = window_instant_for(resolved_now)
        # The shared, lifespan-owned provider: nothing here constructs a
        # provider or opens a connection.
        provider = get_options_provider(settings, override="ibkr", db=db)
        # Deadline guard: measured from the real wall clock, not the legal
        # window timestamp, so a late-starting run still stops in time.
        deadline = decision_deadline_for(job_started_at)
        phase_job_ids = {
            "settlement": V4_SHADOW_SETTLEMENT_JOB_ID,
            "decision": V4_SHADOW_DECISION_JOB_ID,
        }

        def _before(phase: str) -> None:
            runs[phase] = start_scheduler_run(db, phase_job_ids[phase])

        def _after(phase: str, result: Any) -> None:
            run = runs[phase]
            if phase == "settlement":
                db.add(
                    _decision_phase_row(
                        phase="settlement",
                        run_id=run.id,
                        scheduled_at=scheduled_at,
                        job_started_at=job_started_at,
                        completed_at=clock(),
                        lock_wait_ms=result.lock_wait_ms_max,
                        outcome="completed",
                        detail=f"settled={result.settled} failed={result.failed} "
                        f"window_missed={result.window_missed} not_due={result.not_due}",
                    )
                )
                db.commit()
                log.info(
                    "v4 forward window / settlement: evaluated=%d settled=%d failed=%d "
                    "window_missed=%d not_due=%d lock_wait_ms_max=%d",
                    result.evaluated,
                    result.settled,
                    result.failed,
                    result.window_missed,
                    result.not_due,
                    result.lock_wait_ms_max,
                )
                finish_scheduler_run(
                    db,
                    run,
                    status=RUN_STATUS_ERROR
                    if result.failed and not result.settled
                    else RUN_STATUS_SUCCESS,
                    items_evaluated=result.evaluated,
                    items_succeeded=result.settled,
                    items_failed=result.failed,
                )
            else:
                db.add(
                    _decision_phase_row(
                        phase="decision",
                        run_id=run.id,
                        scheduled_at=scheduled_at,
                        job_started_at=job_started_at,
                        completed_at=clock(),
                        lock_wait_ms=result.market_data_lock_wait_ms,
                        outcome="completed",
                        detail=f"ranked={result.ranked} no_action={result.no_action} "
                        f"failed={result.failed} research_not_ready={result.research_not_ready} "
                        f"deadline_skipped={result.deadline_skipped}",
                    )
                )
                db.commit()
                log.info(
                    "v4 forward window / decisions: evaluated=%d ranked=%d no_action=%d "
                    "already=%d research_not_ready=%d failed=%d deadline_skipped=%d "
                    "lock_wait_ms=%d",
                    result.evaluated,
                    result.ranked,
                    result.no_action,
                    result.already_generated,
                    result.research_not_ready,
                    result.failed,
                    result.deadline_skipped,
                    result.market_data_lock_wait_ms,
                )
                finish_scheduler_run(
                    db,
                    run,
                    # A NO_ACTION or RESEARCH_NOT_READY outcome is NOT a failed
                    # run -- only genuine failures count here.
                    status=RUN_STATUS_ERROR
                    if result.failed and not result.ranked
                    else RUN_STATUS_SUCCESS,
                    items_evaluated=result.evaluated,
                    items_succeeded=result.ranked + result.no_action,
                    items_failed=result.failed,
                )

        run_forward_window(
            db,
            settings,
            provider=provider,
            now=resolved_now,
            clock=clock,
            deadline=deadline,
            scheduled_at=scheduled_at,
            job_started_at=job_started_at,
            before_phase=_before,
            after_phase=_after,
            on_telemetry=lambda t: _persist_telemetry(db, t, _current_run_id(runs)),
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 -- a V4 failure must never reach the scheduler
        log.error("v4 forward window job failed", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        for run in runs.values():
            if run.finished_at is None:
                finish_scheduler_run(db, run, status=RUN_STATUS_ERROR, error_summary=str(exc))
    finally:
        db.close()


def _current_run_id(runs: dict[str, Any]) -> int | None:
    run = runs.get("decision") or runs.get("settlement")
    return int(run.id) if run is not None else None


def _decision_phase_row(
    *,
    phase: str,
    run_id: int,
    scheduled_at: datetime,
    job_started_at: datetime,
    completed_at: datetime,
    lock_wait_ms: int | None,
    outcome: str,
    detail: str | None = None,
):
    from models.v4_shadow import V4ForwardWindowTelemetry  # noqa: PLC0415

    return V4ForwardWindowTelemetry(
        phase=phase,
        scheduler_run_id=run_id,
        shadow_decision_id=None,
        symbol=None,
        scheduled_at=scheduled_at,
        job_started_at=job_started_at,
        due_detected_at=None,
        market_data_requested_at=None,
        market_data_acquired_at=None,
        first_contract_request_at=None,
        required_side_ready_at=None,
        completed_at=completed_at,
        lock_wait_ms=lock_wait_ms,
        total_ms=_ms(job_started_at, completed_at),
        outcome=outcome,
        detail=detail,
    )


V4_EOD_SETTLEMENT_FALLBACK_JOB_ID = "v4_eod_settlement_fallback"


def run_v4_eod_settlement_fallback_job(*, now: datetime | None = None) -> None:
    """The post-close job that stops a position being stranded by an empty
    book (authorized 2026-09-04).

    The 15:30 window settles at real executable prices and is unchanged.
    This runs after the close, over the SAME session's still-unsettled due
    configurations only, and applies the explicit end-of-day hierarchy in
    services/v4_settlement_fallback.py: a captured executable side first,
    then that contract's own same-session closing mark, then -- only for a
    contract expiring that day -- expiration intrinsic against the official
    underlying close.

    Append-only and idempotent: a configuration that already has a settled
    row is skipped, so a repeat run is a no-op. Never raises; every outcome
    is recorded as run evidence.
    """
    from services.v4_emergency_settlement import recover_due_settlements  # noqa: PLC0415

    resolved_now = now or datetime.now(UTC)
    db = SessionLocal()
    run = None
    try:
        settings = get_settings()
        run = start_scheduler_run(db, V4_EOD_SETTLEMENT_FALLBACK_JOB_ID)
        if not settings.v4_shadow_enabled:
            finish_scheduler_run(db, run, status=RUN_STATUS_SKIPPED)
            return
        from providers.factory import get_options_provider  # noqa: PLC0415

        provider = get_options_provider(settings, override="ibkr", db=db)
        from analytics.earnings_timing import EASTERN  # noqa: PLC0415

        session_date = resolved_now.astimezone(EASTERN).date()
        # The market-data lock covers the quote/closing-mark sweep, exactly
        # as the 15:30 window's own settlement phase does.
        with V4_MARKET_DATA_LOCK:
            summary = recover_due_settlements(
                db,
                provider=provider,
                session_date=session_date,
                now=resolved_now,
                dry_run=False,
            )
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_SUCCESS,
            items_evaluated=summary.candidates_considered,
            items_succeeded=summary.settled,
            items_failed=summary.unresolved,
        )
    except Exception as exc:  # noqa: BLE001 -- a fallback failure must never take the scheduler down
        log.exception("v4 end-of-day settlement fallback failed")
        if run is not None:
            with contextlib.suppress(Exception):
                finish_scheduler_run(
                    db,
                    run,
                    status=RUN_STATUS_ERROR,
                    error_summary=f"{type(exc).__name__}: {exc}",
                )
    finally:
        db.close()
