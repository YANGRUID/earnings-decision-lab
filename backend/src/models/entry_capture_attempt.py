"""Phase 4.4 -- one official benchmark entry capture attempt, spanning
every leg of a decision_snapshot's recommended strategy. This is the
resolution to Phase 4.3's flagged architecture issue 0A: DecisionSnapshot
must stay immutable forever, including its ``status`` column, so
lifecycle (has this decision been entered? settled?) is derived by
querying immutable child records, never by mutating the snapshot itself.

This table is that queryable record for the "entered" question:

    DecisionSnapshot
        |
        +-- EntryCaptureAttempt exists with status=CAPTURED?
                |
                +-- yes -> this decision IS an entered benchmark
                            observation (see services/decision_lifecycle.py)
                +-- no  -> still pending, or every attempt so far failed

Deliberately NOT a mutable "workflow state" row -- evaluated and
rejected. A genuinely mutable execution-state table (created once,
updated as a job progresses) was considered and is unnecessary here:
each attempt's final outcome (CAPTURED or FAILED) is only known once the
whole attempt concludes, so this row is written exactly once, fully
formed, after the fact -- append-only and immutable, exactly like every
other Phase 4 snapshot table (same reject_snapshot_update() trigger).
Retries are new rows, never updates to an old one -- see the partial
unique index below for the real, DB-level idempotency guarantee this
gives (at most one CAPTURED attempt per decision+portfolio; FAILED
attempts may repeat freely).

Also the natural home for two things that don't belong on the per-leg
EntrySnapshot rows this attempt groups together (see that module): the
attempt-level underlying market context (Phase 4.4 sec 7) and the
deterministic multi-leg entry cost/sizing summary (Phase 4.4 sec 9),
computed once from the individual conservative leg fills via the
existing analytics/options/payoff.py + analytics/decision/budget.py
engines -- never a second, parallel options-math implementation.
"""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import CaptureStatus

if TYPE_CHECKING:
    from models.benchmark_portfolio import BenchmarkPortfolio
    from models.decision_snapshot import DecisionSnapshot
    from models.entry_snapshot import EntrySnapshot

NUM = Numeric(18, 6)


class EntryCaptureAttempt(Base):
    """Grain: one row per (decision_snapshot, benchmark_portfolio,
    attempt) -- indexed, deliberately NOT unique on
    (decision_snapshot_id, benchmark_portfolio_id) alone (append-only
    retries), but a real partial unique index enforces at most one
    status=CAPTURED row per that pair -- see __table_args__.
    """

    __tablename__ = "entry_capture_attempt"
    __table_args__ = (
        # The real, DB-level idempotency guarantee: at most one
        # status=CAPTURED row per (decision_snapshot_id,
        # benchmark_portfolio_id) pair. FAILED/PENDING rows are
        # unrestricted -- retries after a failure are always allowed.
        # CaptureStatus is stored by its member name (SQLAlchemy's
        # default for a Python enum column), hence the uppercase literal.
        Index(
            "uq_entry_capture_attempt_one_captured_per_decision_portfolio",
            "decision_snapshot_id",
            "benchmark_portfolio_id",
            unique=True,
            postgresql_where=text("status = 'CAPTURED'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("decision_snapshot.id"), index=True
    )
    benchmark_portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("benchmark_portfolio.id"), index=True
    )

    status: Mapped[CaptureStatus] = mapped_column(
        Enum(CaptureStatus, name="capture_status", create_type=False),
        default=CaptureStatus.PENDING,
    )
    capture_error: Mapped[str | None] = mapped_column(Text)

    # --- Underlying market context (Phase 4.4 sec 7) -----------------
    underlying_price: Mapped[Decimal | None] = mapped_column(NUM)
    underlying_bid: Mapped[Decimal | None] = mapped_column(NUM)
    underlying_ask: Mapped[Decimal | None] = mapped_column(NUM)
    underlying_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    option_market_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Multi-leg entry cost (Phase 4.4 sec 9) -- from
    # analytics.decision.budget.compute_budget_fit over the real captured
    # legs, never recomputed a second, inconsistent way. -------------
    net_entry_price_per_share: Mapped[Decimal | None] = mapped_column(NUM)
    net_entry_cash: Mapped[Decimal | None] = mapped_column(NUM)
    contracts: Mapped[int | None] = mapped_column(Integer)
    initial_max_risk: Mapped[Decimal | None] = mapped_column(NUM)
    capital_utilization: Mapped[Decimal | None] = mapped_column(NUM)

    source_provider: Mapped[str | None] = mapped_column(String(64))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    decision_snapshot: Mapped["DecisionSnapshot"] = relationship()  # noqa: F821
    benchmark_portfolio: Mapped["BenchmarkPortfolio"] = relationship()  # noqa: F821
    legs: Mapped[list["EntrySnapshot"]] = relationship(  # noqa: F821
        back_populates="capture_attempt"
    )

    @property
    def market_data_quality_label(self) -> str:
        """Phase 4 market-data-quality hardening (2026-08-26), Section 17
        -- VERIFIED_LIVE / DELAYED_DATA / UNKNOWN_QUALITY, derived fresh
        from this attempt's own real per-leg quality values, never
        invisibly combined with a differently-sourced capture and never
        labeled "live" unless every real value present genuinely was. A
        plain Python property, not a stored column: computed on read
        from data already frozen on this row's legs, so it can never
        itself go stale. Phase 4.4 never froze the underlying quote's OWN
        quality flag as a column (only its price/bid/ask/timestamp), so
        this is necessarily option-legs-only -- honest given what this
        row actually has, not a claim about the underlying's quality."""
        from analytics.market_data_policy import derive_capture_quality_label

        values = [
            leg.market_data_quality.value if leg.market_data_quality else None for leg in self.legs
        ]
        return derive_capture_quality_label(values)

    def __repr__(self) -> str:
        return (
            f"EntryCaptureAttempt(decision_snapshot_id={self.decision_snapshot_id}, "
            f"status={self.status!r})"
        )
