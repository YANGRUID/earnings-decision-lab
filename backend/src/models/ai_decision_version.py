"""Point-in-time AI Options Decision journal (Phase 14.9). Append-only,
exactly like AIThesisVersion: a new generation is always a new row, never
an edit to a prior one -- see services/decision_history.py. Persists
enough real provenance (which snapshot rows were used, the exact
candidate/legs the recommendation was for, the deterministic score
breakdown) to recreate exactly what the system believed at generation
time, and enough real post-earnings data (once available) to settle it
honestly -- see services/decision_settlement.py. Settlement fields stay
null until a real settlement actually runs; they are never backfilled
with guessed or partial values.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import DecisionDirection, DecisionSource, DecisionStatus, DecisionVolatilityView
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.company import Company
    from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
    from models.volatility_snapshot import VolatilitySnapshot

NUM = Numeric(18, 6)


class AIDecisionVersion(TimestampMixin, Base):
    __tablename__ = "ai_decision_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)

    direction: Mapped[DecisionDirection] = mapped_column(
        Enum(DecisionDirection, name="decision_direction")
    )
    volatility_view: Mapped[DecisionVolatilityView] = mapped_column(
        Enum(DecisionVolatilityView, name="decision_volatility_view")
    )
    confidence_score: Mapped[int] = mapped_column(Integer)
    confidence_components: Mapped[dict] = mapped_column(JSON)

    rationale: Mapped[str] = mapped_column(Text)
    bull_case: Mapped[str] = mapped_column(Text)
    bear_case: Mapped[str] = mapped_column(Text)
    key_catalysts: Mapped[str] = mapped_column(Text)
    key_risks: Mapped[str] = mapped_column(Text)
    disclaimer: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON)

    decision_source: Mapped[DecisionSource] = mapped_column(
        Enum(DecisionSource, name="decision_source"), default=DecisionSource.AI
    )
    risk_preference: Mapped[str] = mapped_column(String(32))
    # Options Decision Engine V3 Part D: the per-decision Risk Profile
    # (conservative/moderate/aggressive) actually applied at generation
    # time -- see analytics/decision/risk_profile.py. Nullable, not
    # backfilled with a guessed value: decisions generated before this
    # column existed genuinely had no per-decision risk profile, only the
    # global risk_preference above.
    risk_profile: Mapped[str | None] = mapped_column(String(32))

    # The real candidate this recommendation was for -- StrategyCandidate
    # is never persisted elsewhere (computed fresh per-request from the
    # live chain, see analytics/options/strategy_candidates.py), so its
    # defining fields are snapshotted directly here rather than as a
    # dangling FK to nothing.
    recommended_strategy_category: Mapped[str | None] = mapped_column(String(32))
    recommended_strategy_legs: Mapped[list | None] = mapped_column(JSON)
    recommended_strategy_analysis: Mapped[dict | None] = mapped_column(JSON)
    recommended_strategy_score: Mapped[int | None] = mapped_column(Integer)
    recommended_strategy_score_components: Mapped[dict | None] = mapped_column(JSON)
    recommended_strategy_why: Mapped[list | None] = mapped_column(JSON)
    recommended_strategy_risks: Mapped[list | None] = mapped_column(JSON)
    # Options Decision Engine V3 Part G -- see analytics/decision/reasoning.py.
    # Fixed at generation time, like recommended_strategy_why/risks above --
    # never recomputed at read time (unlike historical_compatibility/
    # estimated_probability, which intentionally use the CURRENT real
    # historical-move sample).
    recommended_strategy_why_expiration: Mapped[list | None] = mapped_column(JSON)
    recommended_strategy_why_strikes: Mapped[list | None] = mapped_column(JSON)
    recommended_strategy_why_risk_profile: Mapped[list | None] = mapped_column(JSON)
    recommended_strategy_why_not_alternative: Mapped[list | None] = mapped_column(JSON)
    # Up to two further candidates (alternative, contrarian) -- same shape
    # as the recommended one, kept as a JSON list of dicts rather than a
    # child table since these are read-only, never queried individually.
    alternative_strategies: Mapped[list | None] = mapped_column(JSON)

    expiration: Mapped[date | None] = mapped_column(Date)
    underlying_price: Mapped[Decimal | None] = mapped_column(NUM)
    implied_move_pct: Mapped[Decimal | None] = mapped_column(NUM)

    # Budget-aware sizing (Phase 14.10 Part G) -- what the owner actually
    # asked for and what the deterministic engine actually recommended at
    # that budget, snapshotted here so the Decision Journal reflects what
    # was really recommended at generation time even if the live chain
    # later changes. All null when no budget was supplied for this
    # generation (Strategy Lab / decisions generated before this feature
    # existed) -- never backfilled with a guess.
    trade_budget: Mapped[Decimal | None] = mapped_column(NUM)
    risk_cap: Mapped[Decimal | None] = mapped_column(NUM)
    risk_cap_is_percent: Mapped[bool | None] = mapped_column(Boolean)
    recommended_quantity: Mapped[int | None] = mapped_column(Integer)
    recommended_capital_at_risk: Mapped[Decimal | None] = mapped_column(NUM)
    budget_infeasible_minimum: Mapped[Decimal | None] = mapped_column(NUM)
    # Phase 14.12: set only when zero real strategy candidates existed
    # *before* budget filtering -- the real options market had nothing
    # computable, independent of budget. Lets the UI distinguish "no market
    # data" from "candidates existed but didn't fit the budget" instead of
    # conflating them into one "not feasible for $X budget" message.
    no_market_data_reason: Mapped[str | None] = mapped_column(Text)

    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))

    earnings_estimate_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("earnings_estimate_snapshot.id")
    )
    volatility_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("volatility_snapshot.id"))

    status: Mapped[DecisionStatus] = mapped_column(
        Enum(DecisionStatus, name="decision_status"), default=DecisionStatus.OPEN
    )
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)

    # Settlement -- populated exactly once, only after real post-earnings
    # price data exists (services/decision_settlement.py). Never guessed,
    # never partially filled: strategy_pnl_available stays False (and
    # strategy_pnl stays null) unless real point-in-time entry/exit option
    # premiums exist, which this project does not yet capture for expired
    # contracts -- see docs/limitations.md.
    earnings_event_id: Mapped[int | None] = mapped_column(ForeignKey("earnings_event.id"))
    actual_next_day_move_pct: Mapped[Decimal | None] = mapped_column(NUM)
    actual_five_day_move_pct: Mapped[Decimal | None] = mapped_column(NUM)
    direction_correct: Mapped[bool | None] = mapped_column(Boolean)
    actual_move_exceeded_implied: Mapped[bool | None] = mapped_column(Boolean)
    breakeven_met: Mapped[bool | None] = mapped_column(Boolean)
    strategy_pnl: Mapped[Decimal | None] = mapped_column(NUM)
    strategy_pnl_available: Mapped[bool] = mapped_column(Boolean, default=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company: Mapped["Company"] = relationship()
    earnings_estimate_snapshot: Mapped["EarningsEstimateSnapshot | None"] = relationship()
    volatility_snapshot: Mapped["VolatilitySnapshot | None"] = relationship()
