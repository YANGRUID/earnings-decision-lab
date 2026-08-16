from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.mixins import TimestampMixin


class Company(TimestampMixin, Base):
    """Grain: one row per covered ticker."""

    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    cik: Mapped[str | None] = mapped_column(String(10), unique=True, index=True)
    sector: Mapped[str | None] = mapped_column(String(128))
    exchange: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    earnings_events: Mapped[list["EarningsEvent"]] = relationship(  # noqa: F821
        back_populates="company", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"Company(ticker={self.ticker!r})"
