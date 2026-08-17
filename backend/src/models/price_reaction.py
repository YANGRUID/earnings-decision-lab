from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.earnings_event import EarningsEvent

NUM = Numeric(18, 6)


class PriceReaction(TimestampMixin, Base):
    """Grain: one row per earnings_event (1:1). Raw observed prices around the
    event; move percentages are stored as reported by the provider alongside
    the prices they were derived from, so they can be independently recomputed
    and audited.
    """

    __tablename__ = "price_reaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    earnings_event_id: Mapped[int] = mapped_column(
        ForeignKey("earnings_event.id"), unique=True, index=True
    )

    close_price_before: Mapped[Decimal | None] = mapped_column(NUM)
    after_hours_price: Mapped[Decimal | None] = mapped_column(NUM)
    next_day_close: Mapped[Decimal | None] = mapped_column(NUM)
    five_day_close: Mapped[Decimal | None] = mapped_column(NUM)

    after_hours_move_pct: Mapped[Decimal | None] = mapped_column(NUM)
    next_day_move_pct: Mapped[Decimal | None] = mapped_column(NUM)
    five_day_move_pct: Mapped[Decimal | None] = mapped_column(NUM)

    source_provider: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    earnings_event: Mapped["EarningsEvent"] = relationship(  # noqa: F821
        back_populates="price_reaction"
    )
