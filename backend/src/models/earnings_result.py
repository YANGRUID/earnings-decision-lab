from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.mixins import TimestampMixin

NUM = Numeric(18, 6)


class EarningsResult(TimestampMixin, Base):
    """Grain: one row per earnings_event (1:1). Reported actuals only —
    surprise, guidance-change deltas, and implied-vs-realised comparisons are
    computed by the analytics layer (Phase 3/4), not stored redundantly here.
    """

    __tablename__ = "earnings_result"

    id: Mapped[int] = mapped_column(primary_key=True)
    earnings_event_id: Mapped[int] = mapped_column(
        ForeignKey("earnings_event.id"), unique=True, index=True
    )

    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    actual_eps: Mapped[Decimal | None] = mapped_column(NUM)
    actual_revenue: Mapped[Decimal | None] = mapped_column(NUM)
    gross_margin: Mapped[Decimal | None] = mapped_column(NUM)
    guidance_text: Mapped[str | None] = mapped_column(String)
    kpis: Mapped[dict | None] = mapped_column(JSON)

    source_provider: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    earnings_event: Mapped["EarningsEvent"] = relationship(back_populates="result")  # noqa: F821
