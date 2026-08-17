from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.mixins import TimestampMixin

NUM = Numeric(18, 6)


class PortfolioPositionSnapshot(TimestampMixin, Base):
    """Grain: one row per (account, snapshot_timestamp, conid) -- a real
    brokerage position at a point in time. Deliberately a separate table
    from OptionsSnapshot/PriceBar: a position is "what the user holds",
    never a market quote, and the two must never be mixed (see
    providers/ibkr_portfolio.py). Append-only, like OptionsSnapshot, so
    portfolio exposure stays auditable over time rather than only showing
    "right now".

    ``account_id_masked`` never holds a real, unmasked IBKR account
    number -- masking happens at the provider boundary
    (providers/ibkr_client.mask_account_id) before this model is ever
    constructed, not here.
    """

    __tablename__ = "portfolio_position_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "account_id_masked",
            "snapshot_timestamp",
            "conid",
            name="uq_portfolio_position_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id_masked: Mapped[str] = mapped_column(String(16), index=True)
    snapshot_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    conid: Mapped[int] = mapped_column(Integer, index=True)
    contract_description: Mapped[str] = mapped_column(String(128))
    asset_class: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[Decimal] = mapped_column(NUM)
    currency: Mapped[str | None] = mapped_column(String(8))

    market_price: Mapped[Decimal | None] = mapped_column(NUM)
    market_value: Mapped[Decimal | None] = mapped_column(NUM)
    average_cost: Mapped[Decimal | None] = mapped_column(NUM)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(NUM)
    realized_pnl: Mapped[Decimal | None] = mapped_column(NUM)

    # Option-only identification, null for non-option positions.
    option_expiry: Mapped[str | None] = mapped_column(String(16))
    option_right: Mapped[str | None] = mapped_column(String(1))
    option_strike: Mapped[Decimal | None] = mapped_column(NUM)

    source_provider: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
