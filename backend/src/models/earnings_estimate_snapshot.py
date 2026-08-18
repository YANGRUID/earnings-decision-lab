from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import RevisionDirection, UpcomingEarningsDateSource
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.company import Company

NUM = Numeric(18, 6)


class EarningsEstimateSnapshot(TimestampMixin, Base):
    """Grain: one row per (company, fiscal_period_end_date, snapshot_timestamp)
    -- analyst consensus for one fiscal period, as it stood when fetched.

    Deliberately keyed by fiscal_period_end_date rather than a
    (fiscal_year, fiscal_quarter) pair tied to EarningsEvent: this project
    never creates a discrete EarningsEvent for a fiscal-Q4/fiscal-year-end
    period (SEC XBRL doesn't cleanly expose a standalone Q4 figure -- see
    docs/limitations.md, Phase 1), and confirmed live (Phase 12) that the
    *next* unreported period for every covered ticker is exactly that
    period. Not linking to earnings_event_id at all means this table works
    correctly whether or not a matching event row exists yet.

    Point-in-time honesty note (real, not assumed): Alpha Vantage's
    EARNINGS_ESTIMATES endpoint returns *today's* consensus for every fiscal
    period it lists, including past ones -- it does not preserve what
    consensus looked like back when a past period was still unreported.
    Ingestion (ingestion/collect_earnings_estimates.py) only ever persists
    entries for periods with no confirmed EarningsResult yet, so every row
    in this table really is a genuine "before the numbers were known"
    expectation snapshot, not a same-day snapshot mislabeled as historical.
    """

    __tablename__ = "earnings_estimate_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "fiscal_period_end_date",
            "snapshot_timestamp",
            name="uq_earnings_estimate_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)

    fiscal_period_end_date: Mapped[date] = mapped_column(Date, index=True)
    horizon: Mapped[str] = mapped_column(String(32))
    snapshot_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # --- EPS estimate ---
    eps_estimate_average: Mapped[Decimal | None] = mapped_column(NUM)
    eps_estimate_high: Mapped[Decimal | None] = mapped_column(NUM)
    eps_estimate_low: Mapped[Decimal | None] = mapped_column(NUM)
    eps_estimate_analyst_count: Mapped[int | None] = mapped_column(Integer)
    # Real fields from the provider (trailing-30-day revision counts) --
    # eps_revision_direction is deterministically derived from these at
    # ingestion time (see analytics/earnings/estimate_revisions.py), not a
    # separate fetched value.
    eps_estimate_revision_up_30d: Mapped[int | None] = mapped_column(Integer)
    eps_estimate_revision_down_30d: Mapped[int | None] = mapped_column(Integer)
    eps_revision_direction: Mapped[RevisionDirection] = mapped_column(
        Enum(RevisionDirection, name="eps_revision_direction"), default=RevisionDirection.UNKNOWN
    )

    # --- Revenue estimate ---
    revenue_estimate_average: Mapped[Decimal | None] = mapped_column(NUM)
    revenue_estimate_high: Mapped[Decimal | None] = mapped_column(NUM)
    revenue_estimate_low: Mapped[Decimal | None] = mapped_column(NUM)
    revenue_estimate_analyst_count: Mapped[int | None] = mapped_column(Integer)
    # The provider gives no trailing-revision-count fields for revenue (only
    # for EPS) -- revenue_revision_direction is derived by comparing this
    # snapshot's revenue_estimate_average against the immediately preceding
    # stored snapshot for the same (company, fiscal_period_end_date), so it
    # only becomes anything other than UNKNOWN once a second real snapshot
    # exists. See analytics/earnings/estimate_revisions.py.
    revenue_revision_direction: Mapped[RevisionDirection] = mapped_column(
        Enum(RevisionDirection, name="revenue_revision_direction"),
        default=RevisionDirection.UNKNOWN,
    )

    # --- Provider's own next-report-date prediction, not SEC-confirmed ---
    estimated_report_date: Mapped[date | None] = mapped_column(Date)
    # Provenance of estimated_report_date specifically -- distinct from
    # source_provider (generic metadata already used elsewhere) because a
    # date needs its own, closed reliability classification: never let a
    # manual or algorithmically-estimated date be read as provider-
    # confirmed just because a row exists. See
    # services/market_expectations.py.
    date_source: Mapped[UpcomingEarningsDateSource] = mapped_column(
        Enum(UpcomingEarningsDateSource, name="upcoming_earnings_date_source"),
        default=UpcomingEarningsDateSource.ALPHA_VANTAGE,
    )

    source_provider: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    company: Mapped["Company"] = relationship()  # noqa: F821
