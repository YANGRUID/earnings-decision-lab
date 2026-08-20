"""A named, real-capital Benchmark Portfolio config (Phase 4) -- what a
forward-tested decision's sizing is measured against. See
PHASE4_ARCHITECTURE_REVIEW.md sec 2.4.

Deliberately minimal for Phase 4.1 (database foundation only): no
risk_profile/expiration_mode/is_active columns yet -- those are added in
a later migration once the phase that actually reads them (Phase 4.3's
freeze_decision_snapshot) is implemented, rather than speculatively now.

Unlike decision_snapshot/entry_snapshot/settlement_snapshot, this row is
genuinely mutable over time (``cash_balance`` moves as capital is
deployed/returned in later phases) -- TimestampMixin's ``updated_at`` is
therefore meaningful here, unlike on the append-only-attempt tables.
"""

from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.mixins import TimestampMixin

NUM = Numeric(18, 6)


class BenchmarkPortfolio(TimestampMixin, Base):
    """Grain: one row per named benchmark portfolio."""

    __tablename__ = "benchmark_portfolio"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    initial_capital: Mapped[Decimal] = mapped_column(NUM)
    cash_balance: Mapped[Decimal] = mapped_column(NUM)

    def __repr__(self) -> str:
        return f"BenchmarkPortfolio(name={self.name!r}, cash_balance={self.cash_balance!r})"
