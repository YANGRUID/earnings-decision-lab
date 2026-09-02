"""Point-in-time AI Benchmark Portfolio decision (Phase 4) -- the
immutable core of the forward-testing infrastructure this phase builds.
See PHASE4_ARCHITECTURE_REVIEW.md sec 2.2/2.3 and
PHASE4.3_ARCHITECTURE_REVIEW.md for the full design, and
services/decision_snapshot_freezing.py for how a row here actually gets
written.

Fully immutable, no exceptions -- a real Postgres BEFORE UPDATE trigger
(reusing entry_snapshot's own reject_snapshot_update(), installed by the
migration that widens this table for Phase 4.3) makes every column
reject an UPDATE, including ``status``.
This is a deliberate change from this table's original Phase 4.1 design
(where ``status`` was meant to roll forward as entry/settlement capture
happened) -- Phase 4.3's own explicit decision is "no UPDATE of frozen
decisions," full stop; a later phase that actually needs to mutate
``status`` will need its own migration to relax this trigger, not
assumed here. TimestampMixin's ``updated_at`` was dropped for the same
reason entry_snapshot/settlement_snapshot never had it: a row that can
never be updated has no meaningful "last updated" moment.

Deliberately denormalized against company -- no ``company_id`` FK.
``ticker``/``company_name`` are frozen copies, so a later correction to
``company`` can never silently change what this row is understood to
have said at generation time. ``earnings_calendar_event_id`` and
``benchmark_portfolio_id`` ARE real FKs (unlike the denormalized fields)
-- Phase 4.3 decision #1: every snapshot traces back to exactly one real
calendar event and exactly one real portfolio, enforced at the DB level
via a unique constraint on the pair (never two snapshots for the same
event+portfolio -- see the "never overwrite" requirement).

Most columns below are nullable, mirroring ai_decision_version's own
real precedent: ``generate_decision()`` can genuinely return no
``recommended`` strategy (no actionable real market data) and this is
still a real, honest, frozen outcome worth keeping on record -- never
guessed or backfilled.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import (
    DecisionDirection,
    DecisionSnapshotStatus,
    DecisionVolatilityView,
    RiskProfile,
)

if TYPE_CHECKING:
    from models.ai_thesis_version import AIThesisVersion
    from models.benchmark_portfolio import BenchmarkPortfolio
    from models.earnings_calendar_event import EarningsCalendarEvent
    from models.entry_snapshot import EntrySnapshot
    from models.settlement_snapshot import SettlementSnapshot

NUM = Numeric(18, 6)


class DecisionSnapshot(Base):
    """Grain: one row per frozen AI Benchmark Portfolio decision, one per
    (earnings_calendar_event, benchmark_portfolio) pair -- see the unique
    constraint below."""

    __tablename__ = "decision_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "earnings_calendar_event_id",
            "benchmark_portfolio_id",
            name="uq_decision_snapshot_event_portfolio",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- Event -----------------------------------------------------
    earnings_calendar_event_id: Mapped[int] = mapped_column(
        ForeignKey("earnings_calendar_event.id"), index=True
    )
    benchmark_portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("benchmark_portfolio.id"), index=True
    )

    ticker: Mapped[str] = mapped_column(String(16), index=True)
    company_name: Mapped[str] = mapped_column(String(255))

    strategy_direction: Mapped[DecisionDirection] = mapped_column(
        Enum(DecisionDirection, name="decision_direction")
    )
    # Free-form, like ai_decision_version.recommended_strategy_category --
    # the strategy engine's candidate set is open-ended (butterflies,
    # condors, spreads, ...), not a fixed short enum. Nullable: null when
    # generate_decision() returned no recommended strategy at all.
    strategy_type: Mapped[str | None] = mapped_column(String(32))

    ai_thesis_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_thesis_version.id"), index=True
    )

    # The real domain moment of generation -- explicitly set by the
    # writer, never server-defaulted (matches every other provider-
    # timestamp column in this codebase, e.g. retrieved_at elsewhere).
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    status: Mapped[DecisionSnapshotStatus] = mapped_column(
        Enum(DecisionSnapshotStatus, name="decision_snapshot_status"),
        default=DecisionSnapshotStatus.PENDING_ENTRY,
    )

    # --- Market snapshot --------------------------------------------
    underlying_price: Mapped[Decimal | None] = mapped_column(NUM)
    # Sourced from the grounding VolatilitySnapshot's near-term ATM IV
    # (see option_snapshot_reference below) -- DecisionResult itself only
    # exposes implied_move_pct (the % move), not a raw IV number.
    implied_volatility: Mapped[Decimal | None] = mapped_column(NUM)
    # New, small classification this phase introduces (no existing V3
    # concept) -- "high"/"normal"/"low" derived from the same
    # VolatilitySnapshot's iv_percentile; see
    # services/decision_snapshot_freezing.py::_classify_volatility_regime.
    # "unknown" when no percentile was available, never guessed.
    volatility_regime: Mapped[str | None] = mapped_column(String(16))
    # DecisionResult.volatility_snapshot_id, renamed to match this
    # table's naming -- the real grounding volatility/options-market
    # snapshot this decision was generated against.
    option_snapshot_reference: Mapped[int | None] = mapped_column(
        ForeignKey("volatility_snapshot.id"), index=True
    )

    # --- Strategy -----------------------------------------------------
    strategy_score: Mapped[int | None] = mapped_column(Integer)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON)
    selected_expiration: Mapped[date | None] = mapped_column(Date)
    legs: Mapped[list | None] = mapped_column(JSON)

    # --- Probability (frozen -- Phase 4.3 decision #2, a deliberate
    # departure from ai_decision_version's live-recomputed equivalent;
    # see PHASE4.3_ARCHITECTURE_REVIEW.md sec 2) --------------------
    estimated_probability: Mapped[Decimal | None] = mapped_column(NUM)
    confidence_interval: Mapped[dict | None] = mapped_column(JSON)
    historical_sample_size: Mapped[int | None] = mapped_column(Integer)
    historical_compatibility: Mapped[dict | None] = mapped_column(JSON)

    # --- Explanation --------------------------------------------------
    why_this_strategy: Mapped[list | None] = mapped_column(JSON)
    why_this_expiration: Mapped[list | None] = mapped_column(JSON)
    why_these_strikes: Mapped[list | None] = mapped_column(JSON)
    why_not_alternatives: Mapped[list | None] = mapped_column(JSON)

    # --- Reproducibility (Phase 4 hardening, 2026-08-26) -------------
    # Additive and nullable, per the same "never guessed or backfilled"
    # rule as every other nullable column above: NULL on every historical
    # (including every Aug 25) row, where this input was generated but
    # never frozen. Populated from here forward by
    # services/decision_snapshot_freezing.py, straight off the real
    # DecisionResult these were already computed onto -- never re-derived
    # or inferred after the fact.
    #
    # The structured LLM classification of direction/volatility used for
    # THIS decision (DecisionResult.view.volatility_view) -- previously
    # only the deterministic ``direction`` survived onto this table;
    # volatility_view materially feeds strategy_scoring's volatility_fit
    # component and was silently unrecoverable for a frozen decision.
    volatility_view: Mapped[DecisionVolatilityView | None] = mapped_column(
        Enum(DecisionVolatilityView, name="decision_volatility_view", create_type=False)
    )
    # The real risk profile actually used to generate this decision
    # (DecisionResult.risk_profile) -- a frozen copy, independent of
    # BenchmarkPortfolio.risk_profile, which is mutable and must never
    # change how an old decision is interpreted (see benchmark_portfolio's
    # own risk_profile column docstring for why it's mutable at all).
    effective_risk_profile: Mapped[RiskProfile | None] = mapped_column(
        Enum(RiskProfile, name="risk_profile", create_type=False)
    )
    # The real deterministic evidence-confidence total (DecisionResult.
    # confidence.total, analytics/decision/confidence.py) -- EVIDENCE
    # STRENGTH, never probability of profit, strategy score, or LLM
    # self-reported confidence (see that module's own docstring). The
    # breakdown preserves the same five named components
    # (ConfidenceComponents.as_dict()) that produced the total.
    deterministic_confidence_score: Mapped[int | None] = mapped_column(Integer)
    deterministic_confidence_breakdown: Mapped[dict | None] = mapped_column(JSON)
    # The real provider/model identity of the LLM call that generated
    # THIS decision's DecisionView (services/decision_engine.py's
    # ``llm.name``/``llm.model`` at the point the structured DecisionView
    # call actually happened) -- distinct from whatever LLM generated the
    # underlying Earnings Thesis. ``decision_pipeline.py`` (the only real
    # caller of freeze_decision_snapshot) never passes a manual direction/
    # volatility override to generate_decision(), so for every real
    # DecisionSnapshot this is always the genuine DecisionView-generation
    # call, never a manually-overridden view misattributed to the LLM.
    decision_llm_provider: Mapped[str | None] = mapped_column(String(64))
    decision_llm_model: Mapped[str | None] = mapped_column(String(128))

    # --- Metadata -------------------------------------------------
    # Version stamps for the deterministic engine / LLM prompt that
    # produced this row -- no existing versioning mechanism for either
    # exists elsewhere in this codebase; see
    # services/decision_snapshot_freezing.py's own module constants.
    engine_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32))
    # Phase 4.3 decision #3: V3's current expiration resolver behavior
    # is kept as-is, not refactored to call the separate scored
    # Expiration Engine -- this column records which mechanism actually
    # produced ``selected_expiration``, for future analysis, without
    # changing that behavior now.
    expiration_source: Mapped[str] = mapped_column(String(32))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    earnings_calendar_event: Mapped["EarningsCalendarEvent"] = relationship()  # noqa: F821
    benchmark_portfolio: Mapped["BenchmarkPortfolio"] = relationship()  # noqa: F821
    ai_thesis_version: Mapped["AIThesisVersion | None"] = relationship()
    entry_snapshots: Mapped[list["EntrySnapshot"]] = relationship(  # noqa: F821
        back_populates="decision_snapshot"
    )
    settlement_snapshots: Mapped[list["SettlementSnapshot"]] = relationship(  # noqa: F821
        back_populates="decision_snapshot"
    )

    def __repr__(self) -> str:
        return f"DecisionSnapshot(ticker={self.ticker!r}, generated_at={self.generated_at!r})"
