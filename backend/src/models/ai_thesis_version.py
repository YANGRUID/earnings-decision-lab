from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.company import Company
    from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
    from models.volatility_snapshot import VolatilitySnapshot


class AIThesisVersion(TimestampMixin, Base):
    """One real, successfully-generated AI Earnings Thesis -- a new row per
    generation, never overwritten (see api/routers/research.py::
    get_earnings_thesis). ``created_at`` (from TimestampMixin) is the real
    generation timestamp; the most recent row per company is "the latest
    thesis", but every prior version stays queryable exactly as it was
    generated -- this is point-in-time research, not a single mutable
    document.

    ``earnings_estimate_snapshot_id``/``volatility_snapshot_id`` are the
    real provenance of which consensus/options snapshot the thesis was
    grounded in (nullable: a thesis can be generated even when one or both
    don't exist yet, see services/earnings_thesis.py). Comparing these ids
    against the *current* latest snapshot ids at read time is how the API
    decides whether "newer research data is available" for a given past
    thesis version -- never inferred from a fixed staleness window.
    """

    __tablename__ = "ai_thesis_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)

    business_context: Mapped[str] = mapped_column(Text)
    historical_earnings_pattern: Mapped[str] = mapped_column(Text)
    guidance_trend: Mapped[str] = mapped_column(Text)
    key_risks: Mapped[str] = mapped_column(Text)
    market_setup: Mapped[str] = mapped_column(Text)
    disclaimer: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON)

    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))

    earnings_estimate_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("earnings_estimate_snapshot.id")
    )
    volatility_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("volatility_snapshot.id"))

    company: Mapped["Company"] = relationship()  # noqa: F821
    earnings_estimate_snapshot: Mapped["EarningsEstimateSnapshot | None"] = relationship()  # noqa: F821
    volatility_snapshot: Mapped["VolatilitySnapshot | None"] = relationship()  # noqa: F821
