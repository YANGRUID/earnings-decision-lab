from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import RevisionDirection
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.earnings_event import EarningsEvent

NUM = Numeric(18, 6)


class EarningsExpectationSnapshot(TimestampMixin, Base):
    """Grain: one row per (earnings_event, snapshot_timestamp, source_provider).

    Point-in-time only: every field here must reflect what was knowable at
    ``snapshot_timestamp``. Never backfilled with later information — see
    docs/data_model.md "Point-in-time semantics".
    """

    __tablename__ = "earnings_expectation_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "earnings_event_id",
            "snapshot_timestamp",
            "source_provider",
            name="uq_expectation_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    earnings_event_id: Mapped[int] = mapped_column(
        ForeignKey("earnings_event.id"), index=True
    )

    snapshot_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # --- Estimates / guidance ---
    consensus_eps: Mapped[Decimal | None] = mapped_column(NUM)
    consensus_revenue: Mapped[Decimal | None] = mapped_column(NUM)
    previous_guidance: Mapped[str | None] = mapped_column(String)
    analyst_revision_direction: Mapped[RevisionDirection] = mapped_column(
        Enum(RevisionDirection, name="revision_direction"), default=RevisionDirection.UNKNOWN
    )

    # --- Market context ---
    stock_price: Mapped[Decimal | None] = mapped_column(NUM)
    market_cap: Mapped[Decimal | None] = mapped_column(NUM)
    sector: Mapped[str | None] = mapped_column(String(128))

    # --- Volatility / options positioning ---
    atm_implied_volatility: Mapped[Decimal | None] = mapped_column(NUM)
    implied_move_pct: Mapped[Decimal | None] = mapped_column(NUM)
    implied_move_absolute: Mapped[Decimal | None] = mapped_column(NUM)
    iv_percentile: Mapped[Decimal | None] = mapped_column(NUM)
    near_term_iv: Mapped[Decimal | None] = mapped_column(NUM)
    next_term_iv: Mapped[Decimal | None] = mapped_column(NUM)
    term_structure_slope: Mapped[Decimal | None] = mapped_column(NUM)
    put_call_open_interest_ratio: Mapped[Decimal | None] = mapped_column(NUM)
    put_call_volume_ratio: Mapped[Decimal | None] = mapped_column(NUM)

    # --- Recent price behaviour ---
    recent_return_5d: Mapped[Decimal | None] = mapped_column(NUM)
    recent_return_20d: Mapped[Decimal | None] = mapped_column(NUM)
    sector_return: Mapped[Decimal | None] = mapped_column(NUM)
    market_return: Mapped[Decimal | None] = mapped_column(NUM)

    # --- Provenance ---
    source_provider: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    earnings_event: Mapped["EarningsEvent"] = relationship(  # noqa: F821
        back_populates="expectation_snapshots"
    )
