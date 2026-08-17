from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.mixins import TimestampMixin

EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5 — see docs/ai_architecture.md


class DocumentChunk(TimestampMixin, Base):
    """Grain: one row per (filing, chunk_index) — a single chunk of a
    filing's parsed text, with its embedding and enough metadata to filter
    and cite it without a join back to ``filing`` for the common case.
    """

    __tablename__ = "document_chunk"
    __table_args__ = (UniqueConstraint("filing_id", "chunk_index", name="uq_document_chunk"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filing.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)

    chunk_index: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(256))
    text: Mapped[str] = mapped_column(String)
    token_count: Mapped[int] = mapped_column(Integer)

    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str] = mapped_column(String(128))

    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    filing: Mapped["Filing"] = relationship()  # noqa: F821
    company: Mapped["Company"] = relationship()  # noqa: F821
