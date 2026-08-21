"""One real captured attempt at pricing a single option leg of a
decision_snapshot's recommended strategy, at (or near) its real entry
moment (Phase 4.4). See PHASE4_ARCHITECTURE_REVIEW.md sec 2.3 for the
full reasoning behind this table's shape, and
models/entry_capture_attempt.py for the attempt-level row every leg here
belongs to.

Insert-only: the migration that creates this table also installs a
Postgres BEFORE UPDATE trigger that rejects every UPDATE outright -- a
retry after a failed/incomplete capture always INSERTS a new row rather
than mutating a prior one, so no capture attempt (successful or not) is
ever silently lost from the record. See models/enums.CaptureStatus for
how the "operative" row for a decision is chosen at read time.

Grain is one row per (capture_attempt, leg) -- a multi-leg strategy's
single capture attempt produces multiple rows sharing one
capture_attempt_id, one per leg (``leg_index``), since each leg has its
own contract/strike/bid/ask/Greeks. This is intentionally per-leg,
unlike SettlementSnapshot (an aggregate, per-attempt row) -- see that
module's own docstring for why the two tables don't mirror each other's
grain.

No TimestampMixin here, deliberately: that mixin's ``updated_at``
(``onupdate=func.now()``) implies a row that changes over time, which
this table's whole design forbids -- only ``created_at`` is meaningful
for a row that can only ever be inserted once.

Deliberately a raw quote store: bid/ask/mid/last/IV/Greeks are exactly
what the market said, never a computed fill price -- ``benchmark_entry_
price`` is the one exception (Phase 4.4 sec 8), a real, separately
stored column precisely so the raw quotes and the official conservative
fill assumption are never confused with each other.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import CaptureStatus, MarketDataQuality, OptionAction, OptionType

if TYPE_CHECKING:
    from models.decision_snapshot import DecisionSnapshot
    from models.entry_capture_attempt import EntryCaptureAttempt

NUM = Numeric(18, 6)


class EntrySnapshot(Base):
    """Grain: one row per (capture_attempt, leg)."""

    __tablename__ = "entry_snapshot"
    __table_args__ = (
        UniqueConstraint("capture_attempt_id", "leg_index", name="uq_entry_snapshot_attempt_leg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Indexed, deliberately NOT unique -- append-only capture attempts
    # (see this module's docstring) and multiple legs per attempt both
    # need more than one row per decision_id.
    decision_id: Mapped[int] = mapped_column(ForeignKey("decision_snapshot.id"), index=True)
    # Groups every leg of one real capture attempt together (Phase 4.4
    # sec 6) -- the FK target itself carries the attempt's own outcome/
    # underlying-context/cost-summary, see models/entry_capture_attempt.py.
    capture_attempt_id: Mapped[int] = mapped_column(
        ForeignKey("entry_capture_attempt.id"), index=True
    )
    # 0-indexed position within decision_snapshot.legs -- lets a reader
    # reconstruct which frozen leg this row corresponds to without
    # relying on insertion order, and is half of this table's real
    # uniqueness guarantee (one row per leg per attempt, not more).
    leg_index: Mapped[int] = mapped_column(Integer)

    status: Mapped[CaptureStatus] = mapped_column(
        Enum(CaptureStatus, name="capture_status", create_type=False),
        default=CaptureStatus.PENDING,
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # --- Contract identity --------------------------------------------
    external_contract_id: Mapped[str | None] = mapped_column(String(32))
    expiration: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(NUM)
    option_type: Mapped[OptionType | None] = mapped_column(
        Enum(OptionType, name="option_type", create_type=False)
    )
    action: Mapped[OptionAction | None] = mapped_column(
        Enum(OptionAction, name="option_action", create_type=False)
    )
    quantity: Mapped[int | None] = mapped_column(Integer)
    multiplier: Mapped[Decimal | None] = mapped_column(NUM)

    # --- Raw quote (never a computed fill price) -----------------------
    bid: Mapped[Decimal | None] = mapped_column(NUM)
    ask: Mapped[Decimal | None] = mapped_column(NUM)
    mid: Mapped[Decimal | None] = mapped_column(NUM)
    last_price: Mapped[Decimal | None] = mapped_column(NUM)
    implied_volatility: Mapped[Decimal | None] = mapped_column(NUM)
    delta: Mapped[Decimal | None] = mapped_column(NUM)
    gamma: Mapped[Decimal | None] = mapped_column(NUM)
    theta: Mapped[Decimal | None] = mapped_column(NUM)
    vega: Mapped[Decimal | None] = mapped_column(NUM)
    market_data_quality: Mapped[MarketDataQuality | None] = mapped_column(
        Enum(MarketDataQuality, name="market_data_quality", create_type=False)
    )
    # Free-form provenance for how this specific quote was actually
    # sourced (e.g. "ibkr_live_snapshot") -- distinct from
    # source_provider (which vendor), matching options_snapshot.py's own
    # pricing_source precedent. Never "reconstructed_*" for an official
    # entry -- Phase 4.4 sec 11 forbids historical reconstruction from
    # ever backing an official capture; this field is where that would
    # show up if it ever slipped through, so it's honestly recorded, not
    # just prevented by convention.
    pricing_source: Mapped[str | None] = mapped_column(String(32))

    # --- Official benchmark fill (Phase 4.4 sec 8) ---------------------
    # entry_price = ASK for a long (BUY) leg, BID for a short (SELL) leg
    # -- the conservative, executable-side assumption. Computed once at
    # capture time and stored here, separate from the raw bid/ask above.
    benchmark_entry_price: Mapped[Decimal | None] = mapped_column(NUM)
    # e.g. "BUY_TO_OPEN_AT_ASK" / "SELL_TO_OPEN_AT_BID" -- which rule
    # actually produced benchmark_entry_price, spelled out rather than
    # left for a reader to re-derive from ``action`` alone.
    pricing_assumption: Mapped[str | None] = mapped_column(String(32))

    capture_error: Mapped[str | None] = mapped_column(Text)
    source_provider: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    decision_snapshot: Mapped["DecisionSnapshot"] = relationship(  # noqa: F821
        back_populates="entry_snapshots"
    )
    capture_attempt: Mapped["EntryCaptureAttempt"] = relationship(  # noqa: F821
        back_populates="legs"
    )

    def __repr__(self) -> str:
        return (
            f"EntrySnapshot(decision_id={self.decision_id}, "
            f"status={self.status!r}, strike={self.strike!r})"
        )
