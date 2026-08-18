from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import OptionsSnapshotAnchor
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.company import Company

NUM = Numeric(18, 6)


class VolatilitySnapshot(TimestampMixin, Base):
    """Grain: one row per (company, snapshot_timestamp, method) — a derived
    aggregate computed from ``OptionsSnapshot`` rows by the analytics layer.
    ``inputs`` retains the raw contracts/parameters used so the calculation is
    reproducible and auditable (method, inputs, expiration used — per the
    documented implied-move methodology in docs/options_methodology.md).
    """

    __tablename__ = "volatility_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "snapshot_timestamp", "method", name="uq_volatility_snapshot"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    earnings_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("earnings_event.id"), index=True
    )

    snapshot_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    method: Mapped[str] = mapped_column(String(64))

    # The earnings date this snapshot was computed against -- set even when
    # no EarningsEvent row exists yet for that date (true for every
    # upcoming period today; see EarningsEstimateSnapshot's docstring for
    # why). Lets a forward snapshot be matched to its eventual realized
    # move purely by date once that event is reported, independent of
    # whether earnings_event_id could be populated at snapshot time.
    target_earnings_date: Mapped[date | None] = mapped_column(Date, index=True)

    near_term_expiration: Mapped[date | None] = mapped_column(Date)
    next_term_expiration: Mapped[date | None] = mapped_column(Date)

    atm_iv_near: Mapped[Decimal | None] = mapped_column(NUM)
    atm_iv_next: Mapped[Decimal | None] = mapped_column(NUM)
    term_structure_slope: Mapped[Decimal | None] = mapped_column(NUM)
    iv_percentile: Mapped[Decimal | None] = mapped_column(NUM)
    implied_move_pct: Mapped[Decimal | None] = mapped_column(NUM)
    implied_move_absolute: Mapped[Decimal | None] = mapped_column(NUM)
    # Plain arithmetic on stored quotes, never a directional sentiment
    # label -- see analytics/options/sentiment.py.
    put_call_open_interest_ratio: Mapped[Decimal | None] = mapped_column(NUM)
    put_call_volume_ratio: Mapped[Decimal | None] = mapped_column(NUM)

    inputs: Mapped[dict | None] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Mirrors OptionsSnapshot.anchor for the chain this was computed from --
    # duplicated here (rather than requiring a join) since this is exactly
    # what Strategy Lab and the Upcoming Earnings tab need to decide how to
    # present a result with no target_earnings_date. See
    # OptionsSnapshot.anchor for why the default below is test/ad-hoc-only.
    anchor: Mapped[OptionsSnapshotAnchor] = mapped_column(
        Enum(OptionsSnapshotAnchor, name="options_snapshot_anchor"),
        default=OptionsSnapshotAnchor.EARNINGS_ANCHORED,
    )

    company: Mapped["Company"] = relationship()  # noqa: F821
