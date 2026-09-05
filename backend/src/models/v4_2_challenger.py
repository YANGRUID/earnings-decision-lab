"""V4.2 CHALLENGER forward evidence.

Separate tables, never a widening of the V4.1 rows. V4.1 is the control and
its evidence must stay exactly what it was; a challenger that could rewrite
the control's record would make the comparison worthless.

Append-only, with the same ``reject_snapshot_update()`` trigger every other
V4 evidence table installs. A challenger decision is written once for a given
(event, methodology version, observation window) and is never edited
afterwards -- a rerun is idempotent, not an overwrite.

Nothing here is wired into the scheduler. Writing these rows requires an
explicit caller; no production path constructs one.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

CHALLENGER_SCHEMA_VERSION = "v4_2_challenger_evidence_v1"


class V42ChallengerDecision(Base):
    """One challenger evaluation of one earnings event.

    Shares the event, the research package and the DecisionView with the V4.1
    control -- ``shadow_decision_id`` points at the control decision that was
    evaluated from the same evidence, so the pair can be compared without
    duplicating any of it.
    """

    __tablename__ = "v4_2_challenger_decision"
    __table_args__ = (
        # Idempotency (Section 41): one challenger decision per event per
        # methodology version per observation window. A rerun with identical
        # inputs finds this row rather than writing a second one.
        UniqueConstraint(
            "earnings_calendar_event_id",
            "gate_version",
            "observed_at",
            name="uq_v4_2_challenger_decision_event_version_window",
        ),
        Index("ix_v4_2_challenger_decision_ticker", "ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    earnings_calendar_event_id: Mapped[int] = mapped_column(
        ForeignKey("earnings_calendar_event.id"), nullable=False, index=True
    )
    #: The control decision built from the SAME evidence, when one exists.
    #: Nullable because the challenger may legitimately be evaluated for an
    #: event on which the control produced nothing.
    shadow_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The point-in-time observation instant the evidence belongs to.
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ---- methodology provenance: every versioned component that could
    # ---- change the answer, so a past decision stays explicable.
    schema_version: Mapped[str] = mapped_column(
        String(48), nullable=False, default=CHALLENGER_SCHEMA_VERSION
    )
    gate_version: Mapped[str] = mapped_column(String(48), nullable=False)
    move_edge_version: Mapped[str] = mapped_column(String(48), nullable=False)
    move_distribution_version: Mapped[str | None] = mapped_column(String(48))
    reaction_anchoring_version: Mapped[str | None] = mapped_column(String(48))
    expiry_ladder_version: Mapped[str | None] = mapped_column(String(48))
    friction_version: Mapped[str | None] = mapped_column(String(48))
    ranking_version: Mapped[str | None] = mapped_column(String(64))
    decision_view_schema_version: Mapped[str | None] = mapped_column(String(48))

    # ---- the frozen historical move context actually used (Section 14).
    # Scalars plus a digest rather than a copy of every observation: the
    # digest proves which events contributed without duplicating them.
    historical_sample_n: Mapped[int | None] = mapped_column(Integer)
    historical_evidence_quality: Mapped[str | None] = mapped_column(String(32))
    historical_timing_quality: Mapped[str | None] = mapped_column(String(32))
    historical_median_abs_move_pct: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    historical_p25_abs_move_pct: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    historical_p75_abs_move_pct: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    historical_source_digest: Mapped[str | None] = mapped_column(String(64))
    historical_source_event_count: Mapped[int | None] = mapped_column(Integer)
    historical_as_of: Mapped[date | None] = mapped_column(Date)

    # ---- the market-relative edge inputs (Section 16).
    implied_move_pct: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    underlying_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    market_data_quality: Mapped[str | None] = mapped_column(String(24))

    # ---- outcome.
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    selected_candidate_id: Mapped[str | None] = mapped_column(String(128))
    no_action_reason: Mapped[str | None] = mapped_column(Text)
    candidates_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ---- operational telemetry (Section 34): proof the challenger stays
    # ---- bounded rather than an assurance that it does.
    total_latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    metadata_request_count: Mapped[int | None] = mapped_column(Integer)
    contract_detail_request_count: Mapped[int | None] = mapped_column(Integer)
    market_data_request_count: Mapped[int | None] = mapped_column(Integer)
    unique_contracts_quoted: Mapped[int | None] = mapped_column(Integer)
    reused_control_contracts: Mapped[int | None] = mapped_column(Integer)

    failure_category: Mapped[str | None] = mapped_column(String(48))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class V42ChallengerCandidate(Base):
    """Every candidate the challenger evaluated, accepted or not.

    The refused ones matter as much as the winner: without them a later
    reader cannot tell whether the gate rejected a thin field or a strong
    one.
    """

    __tablename__ = "v4_2_challenger_candidate"
    __table_args__ = (
        UniqueConstraint(
            "challenger_decision_id",
            "candidate_id",
            name="uq_v4_2_challenger_candidate_one_per_decision",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    challenger_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_2_challenger_decision.id"), nullable=False, index=True
    )
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy: Mapped[str] = mapped_column(String(48), nullable=False)
    expiration: Mapped[date] = mapped_column(Date, nullable=False)
    #: Which rung of the bounded ladder this expiry came from; 0 is the
    #: expiry V4.1 would have chosen.
    expiry_ladder_position: Mapped[int | None] = mapped_column(Integer)
    entry_dte: Mapped[int | None] = mapped_column(Integer)
    dte_at_settlement: Mapped[int | None] = mapped_column(Integer)
    settlement_risk: Mapped[str | None] = mapped_column(String(48))
    geometry_variant_id: Mapped[str | None] = mapped_column(String(64))

    semantic_compatibility: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    semantic_tier: Mapped[str | None] = mapped_column(String(24))

    # ---- modeled T+1 economics, carried verbatim from the shared valuation.
    core_median_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    core_worst_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    core_best_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    core_positive_scenario_fraction: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    no_profitable_region: Mapped[bool | None] = mapped_column(Boolean)

    # ---- move edge (Section 16/19).
    move_edge_status: Mapped[str | None] = mapped_column(String(32))
    move_edge_exposure: Mapped[str | None] = mapped_column(String(16))
    move_edge_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    move_edge_threshold: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    move_edge_explanation: Mapped[str | None] = mapped_column(Text)

    # ---- liquidity evidence (Section 26). Collection only: no thresholds
    # ---- are applied to these beyond the gate's existing spread bound.
    mean_relative_spread: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    worst_relative_spread: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    min_bid_size: Mapped[int | None] = mapped_column(Integer)
    min_ask_size: Mapped[int | None] = mapped_column(Integer)
    total_volume: Mapped[int | None] = mapped_column(Integer)
    min_open_interest: Mapped[int | None] = mapped_column(Integer)
    legs_with_empty_bid: Mapped[int | None] = mapped_column(Integer)
    capital_utilisation: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    entry_cash_required: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    # ---- gate outcome.
    viability_acceptable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    viability_reason_codes: Mapped[list | None] = mapped_column(JSON)
    viability_detail: Mapped[list | None] = mapped_column(JSON)
    rank: Mapped[int | None] = mapped_column(Integer)
    legs_json: Mapped[dict | None] = mapped_column(JSON)
    market_data_quality: Mapped[str | None] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class V42ChallengerConfigResult(Base):
    """One configuration's own challenger outcome over the shared candidate
    set (Sections 43-44). A configuration may decline what another actions."""

    __tablename__ = "v4_2_challenger_config_result"
    __table_args__ = (
        UniqueConstraint(
            "challenger_decision_id",
            "configuration_key",
            name="uq_v4_2_challenger_config_one_per_decision",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    challenger_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_2_challenger_decision.id"), nullable=False, index=True
    )
    configuration_key: Mapped[str] = mapped_column(String(48), nullable=False)
    capital_base: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    risk_profile: Mapped[str] = mapped_column(String(24), nullable=False)
    max_risk_dollars: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    selected_candidate_id: Mapped[str | None] = mapped_column(String(128))
    no_action_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class V4ChainMetadataSnapshot(Base):
    """The listed option metadata that existed at one decision instant.

    Deliberately NOT challenger-specific: it is shared point-in-time evidence
    about the market, and freezing it is what makes a multi-expiry replay
    possible at all. The seven historical events have none, which is exactly
    why their multi-expiry behaviour CANNOT_REPLAY_HONESTLY.

    Metadata only -- expirations and listed strikes -- never quotes. It comes
    from the security-definition request the provider already makes, so
    freezing it costs no additional market-data subscription.
    """

    __tablename__ = "v4_chain_metadata_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "earnings_calendar_event_id",
            "observed_at",
            name="uq_v4_chain_metadata_event_window",
        ),
        Index("ix_v4_chain_metadata_ticker", "ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    earnings_calendar_event_id: Mapped[int] = mapped_column(
        ForeignKey("earnings_calendar_event.id"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    underlying_conid: Mapped[str | None] = mapped_column(String(32))
    trading_class: Mapped[str | None] = mapped_column(String(32))
    exchange: Mapped[str | None] = mapped_column(String(32))
    multiplier: Mapped[str | None] = mapped_column(String(16))

    #: Every listed expiration the provider reported, in ISO form.
    available_expirations: Mapped[list | None] = mapped_column(JSON)
    #: Listed strikes, keyed by expiration, for the CONSIDERED expiries only.
    #: Strikes are a chain-wide list in IBKR's security definition, so this
    #: records what was listed without implying any of them were quoted.
    listed_strikes: Mapped[dict | None] = mapped_column(JSON)
    #: The bounded ladder actually considered, with each rung's DTE and risk.
    considered_expirations: Mapped[list | None] = mapped_column(JSON)

    source_provider: Mapped[str | None] = mapped_column(String(64))
    metadata_quality: Mapped[str | None] = mapped_column(String(24))
    expiry_ladder_version: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
