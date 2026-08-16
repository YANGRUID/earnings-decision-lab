from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import GreeksSource, OptionType
from models.mixins import TimestampMixin

NUM = Numeric(18, 6)


class OptionsSnapshot(TimestampMixin, Base):
    """Grain: one row per (company, snapshot_timestamp, expiration, strike,
    option_type, source_provider) — a single contract quote at a point in
    time. Raw ingestion table; ``VolatilitySnapshot`` holds the derived
    per-ticker aggregates computed from rows here.
    """

    __tablename__ = "options_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "snapshot_timestamp",
            "expiration_date",
            "strike",
            "option_type",
            "source_provider",
            name="uq_options_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    earnings_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("earnings_event.id"), index=True
    )

    snapshot_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expiration_date: Mapped[date] = mapped_column(Date, index=True)
    strike: Mapped[Decimal] = mapped_column(NUM)
    option_type: Mapped[OptionType] = mapped_column(Enum(OptionType, name="option_type"))

    bid: Mapped[Decimal | None] = mapped_column(NUM)
    ask: Mapped[Decimal | None] = mapped_column(NUM)
    last_price: Mapped[Decimal | None] = mapped_column(NUM)
    volume: Mapped[int | None] = mapped_column(Integer)
    open_interest: Mapped[int | None] = mapped_column(Integer)

    implied_volatility: Mapped[Decimal | None] = mapped_column(NUM)
    delta: Mapped[Decimal | None] = mapped_column(NUM)
    gamma: Mapped[Decimal | None] = mapped_column(NUM)
    theta: Mapped[Decimal | None] = mapped_column(NUM)
    vega: Mapped[Decimal | None] = mapped_column(NUM)
    greeks_source: Mapped[GreeksSource | None] = mapped_column(
        Enum(GreeksSource, name="greeks_source")
    )

    source_provider: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    company: Mapped["Company"] = relationship()  # noqa: F821
