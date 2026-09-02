"""V4.5 -- EXPERIMENTAL V4 shadow scheduler jobs.

Deliberately a SEPARATE module from services/scheduler.py. That module is
the official V3 pipeline's own entry point, and this project's V4
isolation tests hold it to a strict rule: it must not reference V4
methodology. Keeping the shadow job bodies here means scheduler.py
contains only registration -- no V4 semantics, no V4 valuation, no V4
ranking -- so the official pipeline stays structurally free of V4 while
the process-wide job registry can still schedule an experimental cohort.

TIMING (Section 35): these run on the SAME legal cron as the official V3
decision/exit jobs. V4 gets no extra time and no post-event advantage.

PRIORITY (Sections 36, 38): these jobs never block, delay, or wait on the
official path. If V4 cannot finish safely it fails or skips; V3 is never
held up for it.
"""

from __future__ import annotations

import logging
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


def _due_candidate_events(db, now: datetime) -> list:
    """The same candidate pre-filter V3 uses, then V3's OWN due
    predicate. Imported rather than reimplemented so V4 can never drift
    into a different (earlier, or post-event) legal window than V3 --
    which is the one thing that would make the cohorts incomparable."""
    from datetime import timedelta  # noqa: PLC0415

    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415
    from services.scheduler import (  # noqa: PLC0415
        _DECISION_CANDIDATE_LOOKAHEAD_DAYS,
    )

    horizon = now.date() + timedelta(days=_DECISION_CANDIDATE_LOOKAHEAD_DAYS)
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

        from providers.factory import get_options_provider  # noqa: PLC0415
        from services.scheduler import _due_for_decision_now  # noqa: PLC0415
        from services.v4_shadow_orchestration import (  # noqa: PLC0415
            default_view_generator,
            run_shadow_decisions_for_due_events,
        )

        resolved_now = now or datetime.now(UTC)
        # Section 9 -- the shared, lifespan-owned provider. The factory
        # returns the existing instance in this process; nothing here
        # constructs a provider or opens a connection.
        provider = get_options_provider(settings, override="ibkr", db=db)

        summary = run_shadow_decisions_for_due_events(
            db,
            settings,
            now=resolved_now,
            provider=provider,
            view_generator=default_view_generator,
            due_predicate=_due_for_decision_now,
            candidate_events=_due_candidate_events(db, resolved_now),
        )
        db.commit()

        log.info(
            "v4 shadow decision run: evaluated=%d ranked=%d no_action=%d "
            "already=%d research_not_ready=%d failed=%d",
            summary.evaluated, summary.ranked, summary.no_action,
            summary.already_generated, summary.research_not_ready, summary.failed,
        )
        finish_scheduler_run(
            db,
            run,
            # A NO_ACTION or RESEARCH_NOT_READY outcome is NOT a failed
            # run (Section 14) -- only genuine failures count here.
            status=RUN_STATUS_ERROR if summary.failed and not summary.ranked
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


def run_v4_shadow_settlement_job(*, now: datetime | None = None) -> None:
    """Observes the legal exit window for every frozen shadow decision
    that is due and not already settled.

    Uses the SAME settlement policy as V3 (first post-earnings trading
    day close, ~15:55 ET) -- V4 gets no easier exit (Section 15)."""
    db = SessionLocal()
    run = start_scheduler_run(db, V4_SHADOW_SETTLEMENT_JOB_ID)
    settled = failed = 0
    try:
        settings = get_settings()
        if not settings.v4_shadow_enabled:
            finish_scheduler_run(db, run, status=RUN_STATUS_SKIPPED)
            return

        from datetime import UTC  # noqa: PLC0415

        from models.v4_shadow import V4ShadowDecision, V4ShadowSettlement  # noqa: PLC0415
        from providers.factory import get_options_provider  # noqa: PLC0415
        from services.v4_shadow_settlement import observe_shadow_settlement  # noqa: PLC0415

        resolved_now = now or datetime.now(UTC)
        provider = get_options_provider(settings, override="ibkr", db=db)

        # Only RANKED decisions with no settlement row yet. Section 51 --
        # exactly one successful settlement per decision; the service
        # itself is idempotent, this just avoids pointless work.
        due = (
            db.query(V4ShadowDecision)
            .filter(V4ShadowDecision.status == "RANKED")
            .filter(
                ~V4ShadowDecision.id.in_(db.query(V4ShadowSettlement.shadow_decision_id))
            )
            .all()
        )
        for decision in due:
            try:
                result = observe_shadow_settlement(
                    db, provider=provider, decision=decision, observed_at=resolved_now
                )
                if result.status == "SETTLED":
                    settled += 1
                else:
                    failed += 1
            except Exception:  # noqa: BLE001 -- one decision must not stop the run
                failed += 1
                log.error(
                    "v4 shadow settlement failed for decision %s", decision.id, exc_info=True
                )
        db.commit()

        log.info("v4 shadow settlement run: settled=%d failed=%d", settled, failed)
        finish_scheduler_run(
            db,
            run,
            status=RUN_STATUS_ERROR if failed and not settled else RUN_STATUS_SUCCESS,
            items_evaluated=len(due),
            items_succeeded=settled,
            items_failed=failed,
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
