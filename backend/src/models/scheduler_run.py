"""Operations Monitor -- real, persisted history of each scheduler job
invocation. Nothing about scheduler job execution was durably recorded
before this: services/scheduler.py's own get_scheduler_status() only ever
tracked "last run" in two module-level dicts, reset on every process
restart. That's fine for a live status pill, but useless for a real
Operations page asking "what happened at each of the last N runs, and
why did company X get skipped."

Deliberately its own table, never bolted onto earnings_calendar_event or
decision_snapshot: this is operational metadata about a scheduler
invocation, not a fact about a company's earnings or a frozen trading
decision. Append-only -- a run row is created when the job starts and
updated exactly once, when it finishes; SchedulerRunEvent children are
insert-only and never touched again.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class SchedulerRun(Base):
    """One row per real scheduler job invocation. ``status`` starts
    "running" (written before the job body executes) and is updated to
    "success" or "error" exactly once, when the job finishes -- so a
    process that dies mid-run leaves an honest, visible "running" row
    behind rather than silently vanishing, which is itself a real
    observability signal (a run that's been "running" for far longer
    than the job ever takes is a stuck/crashed process, not a mystery)."""

    __tablename__ = "scheduler_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Matches services/scheduler.py's own CALENDAR_SYNC_JOB_ID etc. --
    # deliberately a plain string, not a DB enum: this table has no
    # FK-like relationship to those constants, and a 5th job later is a
    # zero-migration addition.
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|success|error
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Real counts from the job's own per-item loop -- never estimated.
    # None (not 0) for jobs that aren't per-item (e.g. the IBKR
    # healthcheck), so the frontend can distinguish "0 items" from "this
    # job doesn't have items."
    items_evaluated: Mapped[int | None] = mapped_column(Integer)
    items_succeeded: Mapped[int | None] = mapped_column(Integer)
    items_failed: Mapped[int | None] = mapped_column(Integer)

    # A short, redacted-safe summary of the job-level exception if the
    # whole run failed (never a raw traceback, never a secret -- see
    # observability/redact.py, reused here exactly as provider error
    # messages already are elsewhere in this codebase).
    error_summary: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    events: Mapped[list["SchedulerRunEvent"]] = relationship(back_populates="scheduler_run")

    def __repr__(self) -> str:
        return f"SchedulerRun(job_id={self.job_id!r}, status={self.status!r})"


class SchedulerRunEvent(Base):
    """One row per real earnings_calendar_event a scheduler run actually
    evaluated -- the "why" behind a run's aggregate counts. Populated by
    instrumenting the existing, unmodified per-event loops in
    services/scheduler.py (decision/entry/exit stages) with a single
    real record of whatever outcome that loop already produced --
    never a second, parallel decision about the event, purely an
    observation of what the real pipeline did.

    Deliberately NOT written to earnings_calendar_event.status or any
    trading-state column -- this table is additive-only, read by the
    Operations Monitor, never read by decision_pipeline.py or any other
    trading-logic module."""

    __tablename__ = "scheduler_run_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    scheduler_run_id: Mapped[int] = mapped_column(ForeignKey("scheduler_run.id"), index=True)
    earnings_calendar_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("earnings_calendar_event.id"), index=True
    )
    # Denormalized for fast display without a join -- the same
    # trade-off models/decision_snapshot.py already makes for
    # ticker/company_name.
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    # Which sub-step of the pipeline this outcome came from -- a single
    # scheduler run can touch a symbol at more than one stage in one
    # pass (e.g. decision, then entry, in the same
    # run_decision_and_entry_capture_job invocation).
    stage: Mapped[str] = mapped_column(String(32))  # decision|entry|settlement
    # Mirrors services/decision_pipeline.py::Outcome and the analogous
    # entry/settlement capture outcomes verbatim -- never a second,
    # differently-worded vocabulary for the same real result.
    outcome: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    scheduler_run: Mapped["SchedulerRun"] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return (
            f"SchedulerRunEvent(symbol={self.symbol!r}, stage={self.stage!r}, "
            f"outcome={self.outcome!r})"
        )
