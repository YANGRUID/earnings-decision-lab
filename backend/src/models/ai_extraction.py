from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.company import Company
    from models.filing import Filing


class AIExtraction(TimestampMixin, Base):
    """Grain: one row per (filing, extraction_type, prompt_version) —
    a single structured-extraction run. ``extracted_data`` is the validated
    Pydantic schema (schemas.extraction), dumped to JSON. ``source_chunk_ids``
    retains exactly which DocumentChunk rows were given to the model, so any
    extracted value can be traced back to the text that produced it —
    the same provenance principle as VolatilitySnapshot.inputs.
    """

    __tablename__ = "ai_extraction"

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filing.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)

    extraction_type: Mapped[str] = mapped_column(String(64), index=True)
    extracted_data: Mapped[dict] = mapped_column(JSON)
    source_chunk_ids: Mapped[list] = mapped_column(JSON)

    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[str | None] = mapped_column(String(16))

    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    filing: Mapped["Filing"] = relationship()  # noqa: F821
    company: Mapped["Company"] = relationship()  # noqa: F821
