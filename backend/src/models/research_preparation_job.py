"""On-demand research preparation job (Phase 14) -- tracks real backend
progress for ``services.research_orchestration.prepare_company_research``
so the frontend can show real, live status instead of an opaque spinner.
Deliberately simple: one row per preparation run, with a JSON list of step
records, not a whole job-queue system -- this remains a personal
application (see docs/engineering_decisions.md, Phase 14).
"""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.mixins import TimestampMixin


class JobStatus(enum.StrEnum):
    RUNNING = "running"
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
