"""A named, real-capital Benchmark Portfolio config (Phase 4) -- what a
forward-tested decision's sizing is measured against. See
PHASE4_ARCHITECTURE_REVIEW.md sec 2.4.

Phase 4.4 resolves the gap Phase 4.1 deliberately left open ("no
risk_profile/expiration_mode/is_active columns yet ... added in a later
migration once the phase that actually reads them is implemented"): this
is that migration. The official benchmark's policy is now real, stored
data -- ``risk_profile``/``expiration_mode``/``entry_policy``/
``exit_policy`` -- not a hardcoded constant in service code (see
services/decision_pipeline.py, which previously hardcoded
RiskProfile.MODERATE and now reads this row instead).

Unlike decision_snapshot/entry_snapshot/settlement_snapshot/
entry_capture_attempt, this row is genuinely mutable over time
(``cash_balance`` moves as capital is deployed/returned in later phases,
and the policy fields themselves could change if the owner ever
reconfigures the benchmark) -- TimestampMixin's ``updated_at`` is
therefore meaningful here, unlike on the append-only-attempt tables.
"""

from decimal import Decimal

from sqlalchemy import Boolean, Enum, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.enums import EntryPolicy, ExitPolicy, ExpirationMode, RiskProfile
from models.mixins import TimestampMixin

NUM = Numeric(18, 6)


class BenchmarkPortfolio(TimestampMixin, Base):
    """Grain: one row per named benchmark portfolio."""

    __tablename__ = "benchmark_portfolio"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    initial_capital: Mapped[Decimal] = mapped_column(NUM)
    cash_balance: Mapped[Decimal] = mapped_column(NUM)

    risk_profile: Mapped[RiskProfile] = mapped_column(
        Enum(RiskProfile, name="risk_profile"), default=RiskProfile.MODERATE
    )
    expiration_mode: Mapped[ExpirationMode] = mapped_column(
        Enum(ExpirationMode, name="expiration_mode"), default=ExpirationMode.AUTO
    )
    entry_policy: Mapped[EntryPolicy] = mapped_column(
        Enum(EntryPolicy, name="entry_policy"), default=EntryPolicy.PRE_EARNINGS_15_55_ET
    )
    exit_policy: Mapped[ExitPolicy] = mapped_column(
        Enum(ExitPolicy, name="exit_policy"),
        default=ExitPolicy.FIRST_POST_EARNINGS_TRADING_DAY_CLOSE,
    )
    # Only an is_active=True portfolio's decisions count as official
    # benchmark observations -- not yet read/enforced anywhere in Phase
    # 4.4's own code (there is only ever one portfolio today), but real,
    # stored policy rather than an assumption, per this phase's own
    # explicit requirement.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self) -> str:
        return f"BenchmarkPortfolio(name={self.name!r}, cash_balance={self.cash_balance!r})"
