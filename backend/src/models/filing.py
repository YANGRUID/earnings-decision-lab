from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import FilingType
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.company import Company


class Filing(TimestampMixin, Base):
    """Grain: one row per SEC filing / earnings material, identified by its
    (globally unique) SEC accession number where one exists. Full-text
    chunking and embeddings for RAG live in Phase 5's document tables, not
    here — this row is the source-of-record for provenance.
    """

    __tablename__ = "filing"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)

    filing_type: Mapped[FilingType] = mapped_column(Enum(FilingType, name="filing_type"))
    filing_date: Mapped[date] = mapped_column(Date, index=True)
    fiscal_period: Mapped[str | None] = mapped_column(String(32))
    accession_number: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    cik: Mapped[str | None] = mapped_column(String(10))
    source_url: Mapped[str] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(512))
    raw_text: Mapped[str | None] = mapped_column(String)

    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    company: Mapped["Company"] = relationship()  # noqa: F821
