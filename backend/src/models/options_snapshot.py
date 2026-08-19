from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import (
    GreeksSource,
    MarketDataQuality,
    OptionsSnapshotAnchor,
    OptionsSnapshotPurpose,
    OptionType,
)
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.company import Company

NUM = Numeric(18, 6)


class OptionsSnapshot(TimestampMixin, Base):
    """Grain: one row per (company, snapshot_timestamp, expiration, strike,
    option_type, source_provider) — a single contract quote at a point in
    time. Raw ingestion table; ``VolatilitySnapshot`` holds the derived
    per-ticker aggregates computed from rows here.
    """

    __tablename__ = "options_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "snapshot_timestamp",
            "expiration_date",
            "strike",
            "option_type",
            "source_provider",
            name="uq_options_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    earnings_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("earnings_event.id"), index=True
    )

    snapshot_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expiration_date: Mapped[date] = mapped_column(Date, index=True)
    strike: Mapped[Decimal] = mapped_column(NUM)
    option_type: Mapped[OptionType] = mapped_column(Enum(OptionType, name="option_type"))

    bid: Mapped[Decimal | None] = mapped_column(NUM)
    ask: Mapped[Decimal | None] = mapped_column(NUM)
    last_price: Mapped[Decimal | None] = mapped_column(NUM)
    volume: Mapped[int | None] = mapped_column(Integer)
    open_interest: Mapped[int | None] = mapped_column(Integer)

    implied_volatility: Mapped[Decimal | None] = mapped_column(NUM)
    delta: Mapped[Decimal | None] = mapped_column(NUM)
    gamma: Mapped[Decimal | None] = mapped_column(NUM)
    theta: Mapped[Decimal | None] = mapped_column(NUM)
    vega: Mapped[Decimal | None] = mapped_column(NUM)
    greeks_source: Mapped[GreeksSource | None] = mapped_column(
        Enum(GreeksSource, name="greeks_source")
    )
    # Only ever set from a real signal a provider returned (e.g. IBKR's
    # market-data-availability flag) -- never guessed. Null for providers
    # with no such signal (e.g. Alpha Vantage).
    market_data_quality: Mapped[MarketDataQuality | None] = mapped_column(
        Enum(MarketDataQuality, name="market_data_quality")
    )
    # The provider's own contract identifier (e.g. IBKR's conid), where one
    # exists -- real provenance for re-querying or auditing exactly which
    # contract this quote came from.
    external_contract_id: Mapped[str | None] = mapped_column(String(32))

    source_provider: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Whether this contract's expiration was chosen relative to a known
    # earnings date, or as a general "nearest practical" snapshot taken
    # with no reliable earnings date on record -- see
    # services/options_analytics.py, which always sets this explicitly at
    # collection time. The default below only covers direct ORM
    # construction that doesn't care about the distinction (tests, ad hoc
    # scripts) -- never relied on by the real collection path.
    anchor: Mapped[OptionsSnapshotAnchor] = mapped_column(
        Enum(OptionsSnapshotAnchor, name="options_snapshot_anchor"),
        default=OptionsSnapshotAnchor.EARNINGS_ANCHORED,
    )

    # Why this snapshot was captured -- see OptionsSnapshotPurpose. Default
    # covers every snapshot this project ever took before the fallback
    # policy (Phase 14.10 Part D) needed to distinguish "deliberately
    # captured at a real close" from everything else; only
    # ingestion/capture_close_snapshot.py ever sets CLOSE.
    purpose: Mapped[OptionsSnapshotPurpose] = mapped_column(
        Enum(OptionsSnapshotPurpose, name="options_snapshot_purpose"),
        default=OptionsSnapshotPurpose.INTRADAY,
        server_default=OptionsSnapshotPurpose.INTRADAY.name,
    )

    company: Mapped["Company"] = relationship()  # noqa: F821
