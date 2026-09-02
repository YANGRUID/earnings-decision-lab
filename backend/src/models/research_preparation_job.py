"""On-demand research preparation job (Phase 14) -- tracks real backend
progress for ``services.research_orchestration.prepare_company_research``
so the frontend can show real, live status instead of an opaque spinner.
Deliberately simple: one row per preparation run, with a JSON list of step
records, not a whole job-queue system -- this remains a personal
application (see docs/engineering_decisions.md, Phase 14).

Pre-live hardening (2026-08-25) evolves this SAME table into the durable
queue for the automatic research-preparation worker (services/research_
preparation_queue.py, workers/research_preparation_worker.py) rather than
adding a second, redundant queue table -- a real row here already tracks
everything a queue entry needs (status, per-step progress); the new
columns below (earnings_calendar_event_id, heartbeat_at, attempt_count,
worker_id) are exactly what's missing for it to also serve as one.
prepare_company_research's own on-demand (Search page) call path is
completely unaffected: it still creates a fresh row with these new
columns simply left null, exactly as before.
"""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.mixins import TimestampMixin


class JobStatus(enum.StrEnum):
    # Pre-live hardening: PENDING (enqueued, not yet claimed by any
    # worker) and INTERRUPTED (was RUNNING, its lease/heartbeat went
    # stale -- the worker process behind it is presumed dead) are both
    # real, reclaimable queue states. Neither is ever set by the
    # on-demand Search-page path, which still goes straight to RUNNING.
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class StepStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PreparationStep(enum.StrEnum):
    """The real, fixed sequence a preparation run executes, in order. A
    step's position in this enum is its display order -- the frontend
    renders steps in this order, not an arbitrary one the backend could
    silently reshuffle.
    """

    COMPANY_IDENTIFIED = "company_identified"
    HISTORICAL_EARNINGS = "historical_earnings"
    PRICE_HISTORY = "price_history"
    EARNINGS_ESTIMATES = "earnings_estimates"
    SEC_FILINGS = "sec_filings"
    FILING_EMBEDDINGS = "filing_embeddings"
    OPTIONS_CHAIN = "options_chain"
    EARNINGS_ANALYSIS = "earnings_analysis"
    # V4-only reset (2026-09-02): the V4 decision gate needs a fresh AI
    # thesis, so preparation generates one as its final step (optional --
    # a research-data step never fails the job because the model was busy).
    AI_THESIS = "ai_thesis"


# Steps whose failure fails the whole preparation run -- without these, the
# research workspace has nothing real to show. Every other step is
# optional: a real, honest partial result (e.g. no IBKR Gateway) still
# opens a usable workspace rather than blocking on it.
REQUIRED_STEPS = frozenset(
    {
        PreparationStep.COMPANY_IDENTIFIED,
        PreparationStep.HISTORICAL_EARNINGS,
        PreparationStep.PRICE_HISTORY,
    }
)

STEP_LABELS: dict[PreparationStep, str] = {
    PreparationStep.COMPANY_IDENTIFIED: "Company identified",
    PreparationStep.HISTORICAL_EARNINGS: "Historical earnings",
    PreparationStep.PRICE_HISTORY: "Price history",
    PreparationStep.EARNINGS_ESTIMATES: "Earnings estimates",
    PreparationStep.SEC_FILINGS: "SEC filings",
    PreparationStep.FILING_EMBEDDINGS: "Filing embeddings",
    PreparationStep.OPTIONS_CHAIN: "Options chain",
    PreparationStep.EARNINGS_ANALYSIS: "Earnings analysis",
}


class ResearchPreparationJob(TimestampMixin, Base):
    __tablename__ = "research_preparation_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus, name="research_job_status"))
    # list[{"step": str, "status": str, "detail": str | None, "updated_at": str (ISO)}]
    # -- plain JSON, not a child table: this is real progress state for one
    # job to poll, not data that's ever queried across jobs.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))

    # Pre-live hardening (2026-08-25) -- queue-specific columns. All
    # nullable/defaulted: a row from the pre-existing on-demand (Search
    # page) path simply never sets any of these, exactly as before.
    #
    # Which real calendar event this row exists to prepare for -- null
    # for an on-demand Search-page row (there is no calendar event, just
    # a ticker someone typed in). This is what lets the claim query
    # order by real decision-window proximity (Section 12) and lets
    # Operations join this row back to its pipeline row.
    earnings_calendar_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("earnings_calendar_event.id"), index=True
    )
    # Updated every real step transition (see services/research_
    # orchestration.py::_set_step) while status=RUNNING -- a stale
    # heartbeat on a RUNNING row is the real, honest signal that the
    # worker process behind it is dead, not a guess based on elapsed
    # wall-clock time alone (a genuinely slow step must never be
    # mistaken for a dead worker).
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which worker instance currently holds (or last held) this row's
    # lease -- purely informational/debugging; correctness never depends
    # on this value (the DB-level claim itself, via FOR UPDATE SKIP
    # LOCKED, is what actually prevents two workers double-claiming).
    worker_id: Mapped[str | None] = mapped_column(String(64))
    # How many real attempts this row has had (incremented on every
    # claim) -- bounds automatic retries (see services/research_
    # preparation_queue.py's own MAX_ATTEMPTS) so a company that keeps
    # genuinely failing doesn't retry forever.
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
