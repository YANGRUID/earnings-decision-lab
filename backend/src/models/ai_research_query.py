from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.company import Company

NUM = Numeric(20, 6)


class AIResearchQuery(TimestampMixin, Base):
    """One real, successfully-generated AI Research answer -- persisted so
    it survives navigation/refresh/restart instead of vanishing the moment
    the user leaves the page. Only ever written after generation succeeds
    (see api/routers/research.py::research_query); a failed generation
    never creates a row here. ``created_at`` (from TimestampMixin) is the
    real generation timestamp -- this table is append-only, never updated.

    ``ticker``/``company_id`` are nullable: the underlying agent
    (agents/orchestrator.py) answers general questions that aren't
    necessarily about one company, so a query asked without a ticker
    context is still real history, just not company-scoped.
    """

    __tablename__ = "ai_research_query"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str | None] = mapped_column(String(16), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)

    question: Mapped[str] = mapped_column(Text)
    answer_markdown: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON)

    intent_category: Mapped[str] = mapped_column(String(64))
    planning_method: Mapped[str] = mapped_column(String(64))
    tool_calls: Mapped[list] = mapped_column(JSON)
    verification_ran: Mapped[bool] = mapped_column(Boolean)
    verification_supported: Mapped[bool | None] = mapped_column(Boolean)
    revised: Mapped[bool] = mapped_column(Boolean)

    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    total_input_tokens: Mapped[int] = mapped_column(Integer)
    total_output_tokens: Mapped[int] = mapped_column(Integer)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(NUM)
    total_duration_ms: Mapped[float] = mapped_column(Float)

    company: Mapped["Company | None"] = relationship()  # noqa: F821
