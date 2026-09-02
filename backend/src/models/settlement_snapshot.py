"""One real settlement attempt for a decision_snapshot, evaluating what
actually happened after its earnings event (Phase 4.5). See
PHASE4_ARCHITECTURE_REVIEW.md sec 2.3 for the full reasoning behind this
table's shape.

Insert-only, exactly like EntrySnapshot: the migration that creates this
table installs a Postgres BEFORE UPDATE trigger rejecting every UPDATE, so
a retry after a failed settlement attempt always INSERTS a new row.
``decision_snapshot.status`` only advances to SETTLED once an operative
(status=CAPTURED) row exists here -- never on a substituted or estimated
value; see models/enums.CaptureStatus.

Grain is one row per (decision, settlement attempt) -- an aggregate
summary of the whole strategy's outcome, unlike EntrySnapshot's one-row-
per-leg grain. Per-leg exit detail was deliberately not requested for
this table; ``option_exit_value``/``realized_pnl`` are the position's
combined values, not decomposed per contract.

No TimestampMixin here, for the same reason as EntrySnapshot: this row
can only ever be inserted once, so ``updated_at`` would be misleading.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import CaptureStatus

if TYPE_CHECKING:
    from models.decision_snapshot import DecisionSnapshot

NUM = Numeric(18, 6)


class SettlementSnapshot(Base):
    """Grain: one row per (decision, settlement attempt)."""

    __tablename__ = "settlement_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Indexed, deliberately NOT unique -- append-only capture attempts,
    # same reasoning as EntrySnapshot.decision_id.
    decision_id: Mapped[int] = mapped_column(ForeignKey("decision_snapshot.id"), index=True)

    status: Mapped[CaptureStatus] = mapped_column(
        Enum(CaptureStatus, name="capture_status", create_type=False),
        default=CaptureStatus.PENDING,
    )

    # Known upfront regardless of whether this attempt succeeds -- this is
    # WHICH real earnings event the attempt is for, not itself a captured
    # outcome.
    earnings_date: Mapped[date] = mapped_column(Date)

    # Short categorical/free-text summary (e.g. "beat"/"miss"/"inline") --
    # kept as a plain string rather than a new enum since the exact
    # taxonomy wasn't specified; can become a formal enum in a later
    # migration once real usage clarifies the value set.
    earnings_result: Mapped[str | None] = mapped_column(String(32))

    price_before: Mapped[Decimal | None] = mapped_column(NUM)
    price_after: Mapped[Decimal | None] = mapped_column(NUM)
    # Percentage move, matching this codebase's existing *_move_pct naming
    # convention (PriceReaction.next_day_move_pct etc.) rather than the
    # brief's bare "realized_move".
    realized_move_pct: Mapped[Decimal | None] = mapped_column(NUM)

    option_exit_value: Mapped[Decimal | None] = mapped_column(NUM)
    realized_pnl: Mapped[Decimal | None] = mapped_column(NUM)

    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    capture_error: Mapped[str | None] = mapped_column(Text)
    source_provider: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    decision_snapshot: Mapped["DecisionSnapshot"] = relationship(  # noqa: F821
        back_populates="settlement_snapshots"
    )

    def __repr__(self) -> str:
        return (
            f"SettlementSnapshot(decision_id={self.decision_id}, "
            f"status={self.status!r}, realized_pnl={self.realized_pnl!r})"
        )
