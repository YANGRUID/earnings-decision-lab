from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.mixins import TimestampMixin

NUM = Numeric(18, 6)


class PriceBar(TimestampMixin, Base):
    """Grain: one row per (ticker, trade_date, source_provider) daily bar.

    Covers both covered companies and reference series (SPY for market
    return, SOXX for sector return) used by the earnings-expectation
    snapshot calculations — ``company_id`` is null for reference series that
    aren't in the ``company`` table.
    """

    __tablename__ = "price_bar"
    __table_args__ = (
        UniqueConstraint("ticker", "trade_date", "source_provider", name="uq_price_bar"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)

    trade_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[Decimal] = mapped_column(NUM)
    high: Mapped[Decimal] = mapped_column(NUM)
    low: Mapped[Decimal] = mapped_column(NUM)
    close: Mapped[Decimal] = mapped_column(NUM)
    volume: Mapped[int] = mapped_column(Integer)

    source_provider: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    company: Mapped["Company | None"] = relationship()  # noqa: F821
