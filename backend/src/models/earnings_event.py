from datetime import date

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import AnnouncementTime
from models.mixins import TimestampMixin


class EarningsEvent(TimestampMixin, Base):
    """Grain: one row per (company, fiscal_year, fiscal_quarter) earnings event.

    This is the hub entity everything else (snapshots, results, options data,
    filings-by-proximity) hangs off of.
    """

    __tablename__ = "earnings_event"
    __table_args__ = (
        UniqueConstraint("company_id", "fiscal_year", "fiscal_quarter", name="uq_earnings_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)

    fiscal_year: Mapped[int] = mapped_column(Integer)
    fiscal_quarter: Mapped[int] = mapped_column(Integer)

    earnings_date: Mapped[date | None] = mapped_column(Date, index=True)
    announcement_time: Mapped[AnnouncementTime] = mapped_column(
        Enum(AnnouncementTime, name="announcement_time"), default=AnnouncementTime.UNKNOWN
    )
    date_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    company: Mapped["Company"] = relationship(back_populates="earnings_events")  # noqa: F821
    expectation_snapshots: Mapped[list["EarningsExpectationSnapshot"]] = relationship(  # noqa: F821
        back_populates="earnings_event", cascade="all, delete-orphan"
    )
    result: Mapped["EarningsResult | None"] = relationship(  # noqa: F821
        back_populates="earnings_event", cascade="all, delete-orphan", uselist=False
    )
    price_reaction: Mapped["PriceReaction | None"] = relationship(  # noqa: F821
        back_populates="earnings_event", cascade="all, delete-orphan", uselist=False
    )

    def __repr__(self) -> str:
        return (
            f"EarningsEvent(company_id={self.company_id}, "
            f"FY{self.fiscal_year}Q{self.fiscal_quarter})"
        )
