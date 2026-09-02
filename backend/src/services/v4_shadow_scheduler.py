"""V4 forward-test scheduler jobs (internal module name kept as v4_shadow).

Deliberately a SEPARATE module from services/scheduler.py. That module is
the official V3 pipeline's own entry point, and this project's V4
isolation tests hold it to a strict rule: it must not reference V4
methodology. Keeping the shadow job bodies here means scheduler.py
contains only registration -- no V4 semantics, no V4 valuation, no V4
ranking -- so the official pipeline stays structurally free of V4 while
the process-wide job registry can still schedule an experimental cohort.

TIMING (V4-only reset, 2026-09-02). The V4 cohort observes under its OWN
versioned clock, ``V4_ACTIVE_TIMING_POLICY`` (v2: decision/entry 15:30 ET,
settlement 15:30 ET on the first post-earnings trading day). Both windows
are derived from ``compute_entry_exit_schedule`` with that policy; the
grace/tolerance constants live in analytics/forward_windows.py -- this
module imports nothing from the retired V3 pipeline.

CONCURRENCY. The decision and settlement jobs both fire at 15:30 ET. They
never share a DB transaction, but they do share the one TWS provider, so a
process-wide lock serialises their market-data sections: the settlement
job (short, a handful of quotes for held positions) takes it first, then
the decision job. Nothing is quoted twice.

DEADLINE GUARD. A decision run stops STARTING new full evaluations at
DECISION_DEADLINE_ET (15:50) and records DEADLINE_SKIPPED evidence for the
due, research-ready events it could not start.

    Found live before activation: this module had reused V3's own due
    predicate, which is keyed to V3's 15:55 entry timestamp. Evaluated at
    the 15:30 cron it selected 0 of the 34 events V3 would see at 15:55 --
    the first natural sample would have been empty. The settlement job
    likewise had no exit-window guard at all and would have "settled" a
    15:30 entry at 15:55 the same afternoon, before the announcement.

PRIORITY (Sections 36, 38): these jobs never block, delay, or wait on the
official path. If V4 cannot finish safely it fails or skips; V3 is never
held up for it.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

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
V4_SHADOW_DECISION_JOB_ID = "v4_shadow_decision"
V4_SHADOW_SETTLEMENT_JOB_ID = "v4_shadow_settlement"

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
    from datetime import timedelta  # noqa: PLC0415

    from analytics.forward_windows import DECISION_CANDIDATE_LOOKAHEAD_DAYS  # noqa: PLC0415
    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415

    horizon = now.date() + timedelta(days=DECISION_CANDIDATE_LOOKAHEAD_DAYS)
    return (
        db.query(EarningsCalendarEvent)
        .filter(EarningsCalendarEvent.earnings_date <= horizon)
        .filter(EarningsCalendarEvent.earnings_date >= now.date() - timedelta(days=2))
        .all()
    )


def run_v4_shadow_decision_job(*, now: datetime | None = None) -> None:
    """Drives the real per-event shadow orchestration for the legal
    decision window.

    Never raises: a V4 failure is recorded as shadow evidence and as a
    scheduler run outcome, and can never propagate into the official V3
    path (Section 4).
    """
    db = SessionLocal()
    run = start_scheduler_run(db, V4_SHADOW_DECISION_JOB_ID)
    try:
        settings = get_settings()
        if not settings.v4_shadow_enabled:
            # Defence in depth: the job is not registered while disabled,
            # but must still refuse to act if it somehow is.
            finish_scheduler_run(db, run, status=RUN_STATUS_SKIPPED)
            return

        from datetime import UTC  # noqa: PLC0415

        from analytics.forward_windows import decision_deadline_for  # noqa: PLC0415
        from providers.factory import get_options_provider  # noqa: PLC0415
        from services.v4_shadow_orchestration import (  # noqa: PLC0415
            default_view_generator,
            run_shadow_decisions_for_due_events,
        )

        resolved_now = now or datetime.now(UTC)
        # Section 9 -- the shared, lifespan-owned provider. The factory
        # returns the existing instance in this process; nothing here
        # constructs a provider or opens a connection.
        provider = get_options_provider(settings, override="ibkr", db=db)

        # Deadline guard: measured from the real wall clock, not the
        # legal window timestamp, so a late-starting run still stops in time.
        deadline = decision_deadline_for(datetime.now(UTC))
        with V4_MARKET_DATA_LOCK:
            summary = run_shadow_decisions_for_due_events(
                db,
                settings,
                now=resolved_now,
                provider=provider,
                view_generator=default_view_generator,
                due_predicate=due_for_v4_decision_now,
                candidate_events=_due_candidate_events(db, resolved_now),
                deadline=deadline,
            )
        db.commit()

        log.info(
            "v4 shadow decision run: evaluated=%d ranked=%d no_action=%d "
            "already=%d research_not_ready=%d failed=%d deadline_skipped=%d",
            summary.evaluated,
            summary.ranked,
            summary.no_action,
            summary.already_generated,
            summary.research_not_ready,
            summary.failed,
            summary.deadline_skipped,
        )
        finish_scheduler_run(
            db,
            run,
            # A NO_ACTION or RESEARCH_NOT_READY outcome is NOT a failed
            # run (Section 14) -- only genuine failures count here.
            status=RUN_STATUS_ERROR
            if summary.failed and not summary.ranked
            else RUN_STATUS_SUCCESS,
            items_evaluated=summary.evaluated,
            items_succeeded=summary.ranked + summary.no_action,
            items_failed=summary.failed,
        )
    except Exception as exc:  # noqa: BLE001 -- V4 must never break V3
        log.error("v4 shadow decision job failed", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        finish_scheduler_run(db, run, status=RUN_STATUS_ERROR, error_summary=str(exc))
    finally:
        db.close()


@dataclass
class SettlementRunSummary:
    """What one settlement pass did. ``not_due`` decisions were left
    untouched for a later run; ``evaluated`` counts only the decisions
    that were inside or past their legal window."""

    evaluated: int = 0
    settled: int = 0
    failed: int = 0
    not_due: int = 0


def settle_due_cohorts(db, *, provider, now: datetime) -> SettlementRunSummary:
    """Six-cohort settlement (activation phase, Sections 11-16): every
    decision with at least one OBSERVED configuration entry that has no
    configuration settlement yet, gated by the legal exit window of its
    OWN calendar event.

    NOT_DUE   -> left pending (a 15:30 entry is never settled the same
                 afternoon; T+1 15:55 ET is the earliest honest exit).
    DUE       -> the cohort service dedupes the unique contracts across
                 all held candidates and issues ONE quote call per
                 expiration group -- never one per configuration.
    MISSED    -> each pending configuration is closed as a terminal
                 SETTLEMENT_WINDOW_MISSED failure. No later quote is ever
                 used as exit evidence, exactly as V3's exit capture
                 refuses a late capture.

    Pure orchestration over ``db``; the scheduler job wraps it in its own
    session/run bookkeeping so this can be tested directly.
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
            EarningsCalendarEvent.id.in_(
                [d.earnings_calendar_event_id for d in pending] or [0]
            )
        )
    }

    for decision in pending:
        try:
            event = events.get(decision.earnings_calendar_event_id)
            if event is None:
                state = SETTLEMENT_WINDOW_MISSED
                detail = (
                    f"calendar event {decision.earnings_calendar_event_id} no longer exists; "
                    "the legal exit window cannot be established"
                )
            else:
                state = v4_settlement_window_state(event, now)
                schedule = v4_schedule_for_event(event)
                detail = (
                    f"legal settlement window {schedule.exit_timestamp.isoformat()} "
                    f"(+{LATE_CUTOFF_GRACE}) had already passed at {now.isoformat()} -- "
                    "no honest live exit observation is possible"
                )

            if state == SETTLEMENT_NOT_DUE:
                summary.not_due += 1
                continue

            summary.evaluated += 1
            if state == SETTLEMENT_DUE:
                with V4_MARKET_DATA_LOCK:
                    result = settle_shadow_decision_cohorts(
                        db,
                        provider=provider,
                        decision=decision,
                        observed_at=now,
                        timing_policy_version=policy_version,
                    )
                summary.settled += result.settled
                summary.failed += result.failed
            else:
                summary.failed += fail_missed_settlement_window(
                    db,
                    decision=decision,
                    observed_at=now,
                    detail=detail,
                    timing_policy_version=policy_version,
                )
        except Exception:  # noqa: BLE001 -- one decision must not stop the run
            summary.failed += 1
            log.error("v4 shadow settlement failed for decision %s", decision.id, exc_info=True)
    return summary


def run_v4_shadow_settlement_job(*, now: datetime | None = None) -> None:
    """Observes the legal exit window for every frozen shadow decision
    that is due and not already settled.

    Settles inside the ACTIVE policy's exit window (15:30 ET on the first
    post-earnings trading day, with the shared early tolerance and late
    grace) and records that policy version on every settlement row."""
    db = SessionLocal()
    run = start_scheduler_run(db, V4_SHADOW_SETTLEMENT_JOB_ID)
    try:
        settings = get_settings()
        if not settings.v4_shadow_enabled:
            finish_scheduler_run(db, run, status=RUN_STATUS_SKIPPED)
            return

        from datetime import UTC  # noqa: PLC0415

        from providers.factory import get_options_provider  # noqa: PLC0415

        resolved_now = now or datetime.now(UTC)
        provider = get_options_provider(settings, override="ibkr", db=db)

        summary = settle_due_cohorts(db, provider=provider, now=resolved_now)
        db.commit()

        log.info(
            "v4 shadow settlement run: evaluated=%d settled=%d failed=%d not_due=%d",
            summary.evaluated,
            summary.settled,
            summary.failed,
            summary.not_due,
        )
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_ERROR
            if summary.failed and not summary.settled
            else RUN_STATUS_SUCCESS,
            items_evaluated=summary.evaluated,
            items_succeeded=summary.settled,
            items_failed=summary.failed,
        )
    except Exception as exc:  # noqa: BLE001 -- V4 must never break V3
        log.error("v4 shadow settlement job failed", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        finish_scheduler_run(db, run, status=RUN_STATUS_ERROR, error_summary=str(exc))
    finally:
        db.close()
