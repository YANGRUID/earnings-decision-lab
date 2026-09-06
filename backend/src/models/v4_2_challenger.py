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
    text,
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

    #: The point-in-time listed metadata this decision's expiry ladder was
    #: built from. NULL means no metadata was frozen, which is the honest
    #: state for a single-expiry evaluation over the control's own candidates.
    chain_metadata_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("v4_chain_metadata_snapshot.id")
    )
    expiries_considered: Mapped[int | None] = mapped_column(Integer)
    multi_expiry_status: Mapped[str | None] = mapped_column(String(32))
    #: Whether the actionable configurations got a frozen position. Kept on the
    #: decision so Operations can distinguish "declined" from "wanted to act
    #: and could not be priced" without joining three tables.
    entry_status: Mapped[str | None] = mapped_column(String(24))

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
    #: The implied move derived from THIS expiry's own ATM straddle, never the
    #: nearest expiry's copied forward (Section 26). NULL when the expiry had
    #: no usable straddle -- honestly absent, never substituted.
    expiry_implied_move_pct: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    expiry_implied_move_source: Mapped[str | None] = mapped_column(String(32))
    chain_metadata_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("v4_chain_metadata_snapshot.id")
    )

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


# ---------------------------------------------------------------------------
# Forward outcome lifecycle (Phase 3).
#
# The shape mirrors the control's, deliberately: ONE candidate-level quote
# observation shared by every configuration that selected that candidate, then
# CONFIGURATION-level position and settlement rows carrying that
# configuration's own quantity, capital and realized result. Quote evidence is
# stored once per unique candidate and phase -- never once per configuration --
# which is what makes "six configurations do not cause six quote sweeps" a
# property of the schema rather than a promise about the code.
#
# Separate tables from the control's throughout. A challenger outcome must
# never be able to appear inside a V4.1 Track Record count, and the surest way
# to guarantee that is for the rows to live somewhere the control's queries
# cannot reach.
# ---------------------------------------------------------------------------

CHALLENGER_ENTRY_CONVENTION = "BUY_AT_ASK_SELL_AT_BID"
CHALLENGER_EXIT_CONVENTION = "CLOSE_LONG_AT_BID_CLOSE_SHORT_AT_ASK"


class V42ChallengerCandidateObservation(Base):
    """Executable quote evidence for ONE challenger candidate at ONE phase.

    ENTRY: buy legs at ASK, sell legs at BID.
    EXIT:  close longs at BID, close shorts at ASK, resolved by frozen conId.

    ``net_executable_value`` is for ONE unit of the structure. Quantity is a
    configuration concern and lives on the configuration rows below.
    """

    __tablename__ = "v4_2_challenger_candidate_observation"
    __table_args__ = (
        UniqueConstraint(
            "challenger_decision_id",
            "candidate_id",
            "phase",
            name="uq_v4_2_challenger_observation_one_per_candidate_phase",
        ),
        Index("ix_v4_2_challenger_observation_decision", "challenger_decision_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    challenger_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_2_challenger_decision.id"), nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(8), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    failure_category: Mapped[str | None] = mapped_column(String(48))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    net_executable_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    pricing_convention: Mapped[str] = mapped_column(String(48), nullable=False)
    #: Worst-wins across legs, in the control's own vocabulary. An observation
    #: priced entirely at executable sides reads EXECUTABLE_BID_ASK; one leg
    #: resolved by a closing mark drags the whole observation to
    #: MARKET_CLOSE_FALLBACK, because the structure is only as executable as
    #: its least executable leg.
    pricing_method: Mapped[str | None] = mapped_column(String(96))
    market_data_quality: Mapped[str | None] = mapped_column(String(24))
    source_provider: Mapped[str | None] = mapped_column(String(64))
    earliest_leg_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_leg_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_leg_timestamp_skew_seconds: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    unique_contract_count: Mapped[int | None] = mapped_column(Integer)
    #: Contracts priced from evidence the CONTROL had already acquired in the
    #: same window. The no-extra-sweep claim is measured here, per observation.
    contracts_shared_with_control: Mapped[int | None] = mapped_column(Integer)
    legs_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class V42ChallengerConfigEntry(Base):
    """A challenger configuration's frozen hypothetical position.

    A configuration that said NO_ACTION gets NO row here at all -- not a row
    with quantity zero. There is no phantom position to settle, so no future
    settlement and no P&L can ever attach to a declined configuration.

    ``frozen_legs_json`` carries the exact contracts, conIds included, so
    settlement resolves by contract identity and cannot re-select a strike,
    change an expiration or resize the position.
    """

    __tablename__ = "v4_2_challenger_config_entry"
    __table_args__ = (
        Index(
            "uq_v4_2_challenger_config_entry_one_per_config",
            "challenger_config_result_id",
            unique=True,
        ),
        Index("ix_v4_2_challenger_config_entry_decision", "challenger_decision_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    challenger_config_result_id: Mapped[int] = mapped_column(
        ForeignKey("v4_2_challenger_config_result.id"), nullable=False
    )
    challenger_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_2_challenger_decision.id"), nullable=False
    )
    candidate_observation_id: Mapped[int] = mapped_column(
        ForeignKey("v4_2_challenger_candidate_observation.id"), nullable=False
    )
    configuration_key: Mapped[str] = mapped_column(String(48), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    standardized_capital: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    capital_used: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    max_risk_per_contract: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    max_risk_used: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    entry_net_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    pricing_convention: Mapped[str] = mapped_column(String(48), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_data_quality: Mapped[str | None] = mapped_column(String(24))
    failure_category: Mapped[str | None] = mapped_column(String(48))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    frozen_legs_json: Mapped[dict | None] = mapped_column(JSON)
    expiration: Mapped[date | None] = mapped_column(Date)
    expiry_ladder_position: Mapped[int | None] = mapped_column(Integer)
    entry_dte: Mapped[int | None] = mapped_column(Integer)
    dte_at_settlement: Mapped[int | None] = mapped_column(Integer)
    timing_policy_version: Mapped[str | None] = mapped_column(String(64))
    methodology_version: Mapped[str | None] = mapped_column(String(64))
    configuration_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class V42ChallengerConfigSettlement(Base):
    """A challenger configuration's realized T+1 result.

    Append-only. A configuration may carry any number of immutable failed
    ATTEMPTS -- a failed attempt is never rewritten, so a later end-of-day
    recovery is appended as a new row that supersedes it -- and at most one
    of them may be SETTLED, which a partial unique index enforces in the
    database rather than by convention.

    Both returns are persisted because they answer different questions: return
    on standardized capital compares configurations on the same denominator,
    return on capital actually used says what the position itself did.
    """

    __tablename__ = "v4_2_challenger_config_settlement"
    __table_args__ = (
        Index(
            "uq_v4_2_challenger_settlement_one_settled_per_config",
            "challenger_config_result_id",
            unique=True,
            postgresql_where=text("status = 'SETTLED'"),
        ),
        Index("ix_v4_2_challenger_settlement_decision", "challenger_decision_id"),
        Index("ix_v4_2_challenger_settlement_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    challenger_config_result_id: Mapped[int] = mapped_column(
        ForeignKey("v4_2_challenger_config_result.id"), nullable=False
    )
    challenger_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_2_challenger_decision.id"), nullable=False
    )
    challenger_config_entry_id: Mapped[int] = mapped_column(
        ForeignKey("v4_2_challenger_config_entry.id"), nullable=False
    )
    candidate_observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("v4_2_challenger_candidate_observation.id")
    )
    configuration_key: Mapped[str] = mapped_column(String(48), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    standardized_capital: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    capital_used: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    entry_net_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    exit_net_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    return_on_standardized_capital: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    return_on_capital_used: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    entry_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pricing_convention: Mapped[str] = mapped_column(String(48), nullable=False)
    pricing_method: Mapped[str | None] = mapped_column(String(96))
    settlement_grade: Mapped[str | None] = mapped_column(String(32))
    recovery_provenance: Mapped[str | None] = mapped_column(String(48))
    supersedes_settlement_id: Mapped[int | None] = mapped_column(
        ForeignKey("v4_2_challenger_config_settlement.id")
    )
    market_data_quality: Mapped[str | None] = mapped_column(String(24))
    failure_category: Mapped[str | None] = mapped_column(String(48))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    timing_policy_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
