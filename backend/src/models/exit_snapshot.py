"""One real captured attempt at pricing a single option leg's *exit* --
closing the exact position a decision_snapshot's EntrySnapshot leg
opened (Phase 4.5). Mirrors models/entry_snapshot.py exactly, on the
closing side; see models/settlement_capture_attempt.py for the
attempt-level row every leg here belongs to.

Insert-only, same mechanism as every other Phase 4 snapshot table: the
migration that creates this table installs a Postgres BEFORE UPDATE
trigger reusing reject_snapshot_update(), rejecting every UPDATE
outright. A retry after a failed/incomplete exit capture always INSERTS
a new row.

Grain is one row per (settlement_attempt, leg) -- mirrors EntrySnapshot's
(capture_attempt, leg) grain exactly, unlike the old, unused
``settlement_snapshot`` (an aggregate, per-attempt row with no per-leg
detail at all -- see that module's own docstring). This project's own
"multi-leg strategy" testing requirement is only honestly satisfiable
with a real, queryable, per-leg exit row -- a 4-leg iron condor's
realized P&L is only auditable if each leg's own BID/ASK exit fill is a
real row, exactly like each leg's own entry fill already is.

No TimestampMixin here, for the same reason as EntrySnapshot: a row that
can only ever be inserted once has no meaningful "last updated" moment.

Deliberately a raw quote store, contract identity copied forward from
the entry leg it closes: bid/ask/mid/last/IV/Greeks are exactly what the
market said at exit, never a computed fill price --
``benchmark_exit_price`` is the one exception (mirrors EntrySnapshot's
own ``benchmark_entry_price``), a real, separately stored column
precisely so the raw exit quote and the official conservative fill
assumption are never confused.
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
    from models.entry_snapshot import EntrySnapshot
    from models.settlement_capture_attempt import SettlementCaptureAttempt

NUM = Numeric(18, 6)


class ExitSnapshot(Base):
    """Grain: one row per (settlement_attempt, leg)."""

    __tablename__ = "exit_snapshot"
    __table_args__ = (
        UniqueConstraint("settlement_attempt_id", "leg_index", name="uq_exit_snapshot_attempt_leg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Indexed, deliberately NOT unique -- append-only capture attempts
    # and multiple legs per attempt both need more than one row per
    # decision_id, same reasoning as entry_snapshot.decision_id.
    decision_id: Mapped[int] = mapped_column(ForeignKey("decision_snapshot.id"), index=True)
    # Groups every leg of one real exit capture attempt together -- the
    # FK target itself carries the attempt's own outcome/underlying-
    # context/P&L summary, see models/settlement_capture_attempt.py.
    settlement_attempt_id: Mapped[int] = mapped_column(
        ForeignKey("settlement_capture_attempt.id"), index=True
    )
    # The exact entry leg this exit closes -- lets a reader compute
    # per-leg realized P&L with one join, never a second lookup to match
    # legs by strike/option_type again.
    entry_snapshot_id: Mapped[int] = mapped_column(ForeignKey("entry_snapshot.id"), index=True)
    # 0-indexed position within decision_snapshot.legs -- matches the
    # corresponding entry_snapshot.leg_index for the same leg.
    leg_index: Mapped[int] = mapped_column(Integer)

    status: Mapped[CaptureStatus] = mapped_column(
        Enum(CaptureStatus, name="capture_status", create_type=False),
        default=CaptureStatus.PENDING,
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # --- Contract identity, copied forward from the entry leg ----------
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

    # --- Raw exit quote (never a computed fill price) ------------------
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
    # Never "historical_last"/"reconstructed_*" for an official exit --
    # this phase never calls the historical-reconstruction path at all
    # (see PHASE4_5_SETTLEMENT_ARCHITECTURE_REVIEW.md's addendum), and
    # this field is where that would honestly show up if it ever did.
    pricing_source: Mapped[str | None] = mapped_column(String(32))

    # --- Official benchmark close ---------------------------------------
    # exit_price = BID for a long (BUY) leg, ASK for a short (SELL) leg
    # -- the conservative, executable-side assumption, mirrored from the
    # opposite side of entry_snapshot's own rule.
    benchmark_exit_price: Mapped[Decimal | None] = mapped_column(NUM)
    # e.g. "SELL_TO_CLOSE_AT_BID" / "BUY_TO_CLOSE_AT_ASK".
    pricing_assumption: Mapped[str | None] = mapped_column(String(32))
    # (exit - entry) * direction_sign, per share -- lets a reader verify
    # the attempt-level realized_pnl by summing real per-leg rows, never
    # a number that must be trusted without an audit trail.
    realized_pnl_per_share: Mapped[Decimal | None] = mapped_column(NUM)

    capture_error: Mapped[str | None] = mapped_column(Text)
    source_provider: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    decision_snapshot: Mapped["DecisionSnapshot"] = relationship()  # noqa: F821
    settlement_attempt: Mapped["SettlementCaptureAttempt"] = relationship(  # noqa: F821
        back_populates="legs"
    )
    entry_snapshot: Mapped["EntrySnapshot"] = relationship()  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"ExitSnapshot(decision_id={self.decision_id}, "
            f"status={self.status!r}, strike={self.strike!r})"
        )
