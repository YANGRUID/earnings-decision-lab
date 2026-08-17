from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.earnings_event import EarningsEvent

NUM = Numeric(18, 6)


class StrategyReplay(TimestampMixin, Base):
    """Grain: one row per (earnings_event, strategy_name, strike_selection_rule)
    — a deterministic reconstruction of how a given options strategy would
    have been entered before a historical earnings event, using only
    information available at entry time.

    ``legs`` and ``breakevens`` are stored as JSON for auditability (exact
    strikes/premiums/rule used), matching the same "store method + inputs"
    principle as VolatilitySnapshot.inputs. Strike selection is rule-based
    (see analytics.options.replay) — never chosen with knowledge of the
    event's actual outcome.
    """

    __tablename__ = "strategy_replay"

    id: Mapped[int] = mapped_column(primary_key=True)
    earnings_event_id: Mapped[int] = mapped_column(ForeignKey("earnings_event.id"), index=True)

    strategy_name: Mapped[str] = mapped_column(String(64))
    strike_selection_rule: Mapped[str] = mapped_column(String(64))
    entry_snapshot_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expiration_date: Mapped[date] = mapped_column(Date)

    legs: Mapped[dict] = mapped_column(JSON)
    net_premium: Mapped[Decimal] = mapped_column(NUM)
    underlying_price_at_entry: Mapped[Decimal] = mapped_column(NUM)

    max_profit: Mapped[Decimal | None] = mapped_column(NUM)
    max_loss: Mapped[Decimal | None] = mapped_column(NUM)
    breakevens: Mapped[list] = mapped_column(JSON)

    underlying_price_at_evaluation: Mapped[Decimal | None] = mapped_column(NUM)
    payoff_at_evaluation: Mapped[Decimal | None] = mapped_column(NUM)

    source_provider: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    earnings_event: Mapped["EarningsEvent"] = relationship()  # noqa: F821
