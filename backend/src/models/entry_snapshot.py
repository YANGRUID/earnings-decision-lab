"""One real captured attempt at pricing a single option leg of a
decision_snapshot's recommended strategy, at (or near) its real entry
moment (Phase 4.4). See PHASE4_ARCHITECTURE_REVIEW.md sec 2.3 for the
full reasoning behind this table's shape.

Insert-only: the migration that creates this table also installs a
Postgres BEFORE UPDATE trigger that rejects every UPDATE outright -- a
retry after a failed/incomplete capture always INSERTS a new row rather
than mutating a prior one, so no capture attempt (successful or not) is
ever silently lost from the record. See models/enums.CaptureStatus for
how the "operative" row for a decision is chosen at read time.

Grain is one row per (decision, capture attempt, leg) -- a multi-leg
strategy's single capture attempt produces multiple rows sharing the
same decision_id and captured_at, one per leg, since each leg has its
own strike/bid/ask/Greeks. This is intentionally per-leg, unlike
SettlementSnapshot (an aggregate, per-attempt row) -- see that module's
own docstring for why the two tables don't mirror each other's grain.

No TimestampMixin here, deliberately: that mixin's ``updated_at``
(``onupdate=func.now()``) implies a row that changes over time, which
this table's whole design forbids -- only ``created_at`` is meaningful
for a row that can only ever be inserted once.

Deliberately a raw quote store: bid/ask/mid/IV/Greeks are exactly what
the market said, never a computed fill price. Which side of the NBBO is
the honest entry cost (ASK for a BUY leg, BID for a SELL leg) is derived
by the caller from ``action``, not stored redundantly here.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import CaptureStatus, OptionAction, OptionType

if TYPE_CHECKING:
    from models.decision_snapshot import DecisionSnapshot

NUM = Numeric(18, 6)


class EntrySnapshot(Base):
    """Grain: one row per (decision, capture attempt, leg)."""

    __tablename__ = "entry_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Indexed, deliberately NOT unique -- append-only capture attempts
    # (see this module's docstring) and multiple legs per attempt both
    # need more than one row per decision_id.
    decision_id: Mapped[int] = mapped_column(ForeignKey("decision_snapshot.id"), index=True)

    status: Mapped[CaptureStatus] = mapped_column(
        Enum(CaptureStatus, name="capture_status"), default=CaptureStatus.PENDING
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    expiration: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(NUM)
    option_type: Mapped[OptionType | None] = mapped_column(
        Enum(OptionType, name="option_type", create_type=False)
    )
    action: Mapped[OptionAction | None] = mapped_column(Enum(OptionAction, name="option_action"))

    bid: Mapped[Decimal | None] = mapped_column(NUM)
    ask: Mapped[Decimal | None] = mapped_column(NUM)
    mid: Mapped[Decimal | None] = mapped_column(NUM)
    implied_volatility: Mapped[Decimal | None] = mapped_column(NUM)
    delta: Mapped[Decimal | None] = mapped_column(NUM)
    gamma: Mapped[Decimal | None] = mapped_column(NUM)
    theta: Mapped[Decimal | None] = mapped_column(NUM)
    vega: Mapped[Decimal | None] = mapped_column(NUM)

    capture_error: Mapped[str | None] = mapped_column(Text)
    source_provider: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    decision_snapshot: Mapped["DecisionSnapshot"] = relationship(  # noqa: F821
        back_populates="entry_snapshots"
    )

    def __repr__(self) -> str:
        return (
            f"EntrySnapshot(decision_id={self.decision_id}, "
            f"status={self.status!r}, strike={self.strike!r})"
        )
