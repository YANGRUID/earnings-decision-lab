"""V4.4C -- immutable V4 shadow forward-test evidence.

WHY SEPARATE TABLES (Section 9). V4 shadow evidence never touches the
official V3 tables. V3 stays the frozen official control cohort, and the
two must be comparable precisely because they are stored apart: same
event identity, same legal timestamp, independently versioned
methodology, separate results. Nothing here is named in a way that
implies brokerage execution -- there is no "fill", no "order", no
"position", because none of those things happen. What is recorded is an
*observation* of real executable quotes.

IMMUTABILITY (Section 8). Every table below is append-only and carries
the project's own existing ``reject_snapshot_update()`` BEFORE UPDATE
trigger -- the same DB-level guard entry_snapshot, decision_snapshot,
exit_snapshot and the capture-attempt tables already use. Retry history
is appended as V4ShadowRunEvent rows; a frozen decision is never mutated.

LABEL LEAKAGE (Section 63). Realized outcome lives ONLY on
V4ShadowSettlement, written at settlement time. V4ShadowDecision and its
candidates are frozen before any outcome exists and have no column
capable of holding one -- so decision-time code cannot read an outcome
even by accident.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

#: Bumped whenever the shadow evidence SHAPE changes, independently of
#: any methodology version (Section 65).
SHADOW_SCHEMA_VERSION = "v4-shadow-schema-v1"


# --------------------------------------------------------------------------
# Failure taxonomy (Section 53). Explicit categories, never free text
# alone -- free text cannot be counted, filtered, or alerted on.
# --------------------------------------------------------------------------

SHADOW_FAILURE_CATEGORIES = (
    "RESEARCH_NOT_READY",
    "VIEW_GENERATION_FAILED",
    "MARKET_DATA_UNAVAILABLE",
    "CHAIN_METADATA_FAILED",
    "NO_VALID_CANDIDATE",
    "QUOTE_INCOMPLETE",
    "VALUATION_FAILED",
    "RANKING_FAILED",
    "ENTRY_OBSERVATION_FAILED",
    "SETTLEMENT_OBSERVATION_FAILED",
    "INTERNAL_ERROR",
)

#: Section 54 -- a valid NO_ACTION is an OUTCOME, not a failure. Kept
#: deliberately out of SHADOW_FAILURE_CATEGORIES so it can never be
#: counted as one, mirroring the same cleanup already done for V3.
SHADOW_DECISION_STATUSES = ("RANKED", "NO_ACTION", "FAILED")


class V4ShadowDecision(Base):
    """One frozen V4 shadow decision for one earnings event, at the same
    legal decision window V3 used. Immutable once written."""

    __tablename__ = "v4_shadow_decision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- SAME-EVENT IDENTITY (Section 46) -- authoritative event id, not
    # a ticker/date match, so V3<->V4 comparison can never pair the wrong
    # event.
    earnings_calendar_event_id: Mapped[int] = mapped_column(
        ForeignKey("earnings_calendar_event.id"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- LEGAL TIMESTAMP (Sections 11, 12) ---
    #: The official decision window this shadow decision belongs to --
    #: identical to the window V3 used, never an earlier one that would
    #: hand V4 a data advantage.
    legal_decision_window_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Point-in-time boundary: no input newer than this may enter.
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Section 74 -- worst skew across all frozen inputs.
    max_input_skew_seconds: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    #: Populated for NO_ACTION -- an outcome, with its exact reason.
    no_action_reason: Mapped[str | None] = mapped_column(Text)
    #: Populated for FAILED -- one of SHADOW_FAILURE_CATEGORIES.
    failure_category: Mapped[str | None] = mapped_column(String(48), index=True)
    failure_detail: Mapped[str | None] = mapped_column(Text)

    # --- FROZEN DECISION VIEW (Sections 4, 73) -- the primary gap V4.4B's
    # historical replay exposed: without this, V4.2 semantics were inert.
    view_direction: Mapped[str | None] = mapped_column(String(16))
    view_volatility: Mapped[str | None] = mapped_column(String(24))
    view_expected_move_intent: Mapped[str | None] = mapped_column(String(24))
    view_confidence: Mapped[str | None] = mapped_column(String(24))
    view_reasoning: Mapped[str | None] = mapped_column(Text)
    #: Structured references to the evidence the view was built from.
    view_evidence_refs: Mapped[dict | None] = mapped_column(JSON)

    # --- PROVENANCE (Section 5) -- reproducibility must not depend on
    # reading environment variables after the fact.
    llm_provider: Mapped[str | None] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    decision_view_schema_version: Mapped[str | None] = mapped_column(String(48))
    # Model/reasoning provenance (migration d2f4b6a81c37, 2026-09-02):
    # configured alias (llm_model) vs API-reported identity, the explicit
    # thinking configuration, and provider-reported usage/latency. No
    # reasoning text is stored anywhere.
    llm_returned_model: Mapped[str | None] = mapped_column(String(128))
    llm_thinking: Mapped[str | None] = mapped_column(String(16))
    llm_reasoning_effort: Mapped[str | None] = mapped_column(String(16))
    llm_max_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_finish_reason: Mapped[str | None] = mapped_column(String(32))
    llm_input_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_output_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_cache_hit_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_latency_ms: Mapped[int | None] = mapped_column(Integer)
    llm_config_version: Mapped[str | None] = mapped_column(String(48))

    # --- MARKET CONTEXT (Sections 21, 55) ---
    underlying_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    underlying_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    market_data_quality: Mapped[str | None] = mapped_column(String(24))
    source_provider: Mapped[str | None] = mapped_column(String(64))

    # --- METHODOLOGY VERSIONS (Sections 19, 65) -- everything needed to
    # reproduce the rank, with no silent drift.
    #: Which clock this observation was taken under (Section 23). Frozen
    #: per record so a reader never has to infer the cohort's timing from
    #: a bare timestamp -- V3 is 15:55 ET, V4 is 15:30 ET, and historical
    #: rows must keep saying what they actually ran under.
    decision_timing_policy_version: Mapped[str | None] = mapped_column(String(64))
    #: V4 consolidation (Section 25): the expected-move context the
    #: candidate geometry was built from -- spot, implied move, +/-1 EM
    #: boundaries, historical median move -- frozen once at event level.
    expected_move: Mapped[dict | None] = mapped_column(JSON)
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)
    shadow_schema_version: Mapped[str] = mapped_column(String(48), nullable=False)
    strategy_semantics_version: Mapped[str | None] = mapped_column(String(48))
    compatibility_version: Mapped[str | None] = mapped_column(String(48))
    expected_move_version: Mapped[str | None] = mapped_column(String(48))
    strike_engine_version: Mapped[str | None] = mapped_column(String(48))
    geometry_version: Mapped[str | None] = mapped_column(String(48))
    valuation_version: Mapped[str | None] = mapped_column(String(48))
    scenario_grid_version: Mapped[str | None] = mapped_column(String(64))
    iv_scenario_version: Mapped[str | None] = mapped_column(String(48))
    ranking_version: Mapped[str | None] = mapped_column(String(64))

    # --- RESULT (Section 24) -- rank 1 is named, but the whole ordered
    # set survives on the candidate rows.
    rank_1_candidate_id: Mapped[str | None] = mapped_column(String(128))
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rankable_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- PERFORMANCE (Sections 34, 71) ---
    total_latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    candidate_generation_ms: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    valuation_ms: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    ranking_ms: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    tws_request_count: Mapped[int | None] = mapped_column(Integer)
    unique_contracts_quoted: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    config_results: Mapped[list[V4ShadowConfigResult]] = relationship(
        back_populates="decision", cascade="all, delete-orphan"
    )
    candidates: Mapped[list[V4ShadowCandidate]] = relationship(
        back_populates="decision", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Section 47 -- idempotency. A scheduler retry must not create a
        # second shadow decision for the same event at the same window
        # under the same engine version.
        UniqueConstraint(
            "earnings_calendar_event_id",
            "legal_decision_window_at",
            "engine_version",
            name="uq_v4_shadow_decision_event_window_engine",
        ),
        Index("ix_v4_shadow_decision_status_window", "status", "legal_decision_window_at"),
    )


class V4ShadowCandidate(Base):
    """Every honestly generated candidate, not merely rank #1 (Section 6).
    Discarding the rest would make future head-to-head analysis
    impossible, which is the whole reason this phase exists."""

    __tablename__ = "v4_shadow_candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shadow_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), nullable=False, index=True
    )

    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy: Mapped[str] = mapped_column(String(48), nullable=False)
    expiration: Mapped[date] = mapped_column(Date, nullable=False)
    geometry_variant_id: Mapped[str | None] = mapped_column(String(64))

    #: None for a non-rankable candidate -- never 0, never a sentinel
    #: that would sort it alongside real ranks.
    rank: Mapped[int | None] = mapped_column(Integer, index=True)
    validity_status: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)

    # --- SEMANTICS (V4.2) ---
    semantic_compatibility: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    semantic_tier: Mapped[str | None] = mapped_column(String(24))
    semantic_reason_codes: Mapped[dict | None] = mapped_column(JSON)

    # --- CORE T+1 ECONOMICS (Section 17) -- computed from V4.4A's
    # original ±1.0 EM grid ONLY. These are the values V4.4B ranks on.
    core_worst_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    core_median_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    core_best_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    core_positive_scenario_fraction: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    core_positive_region_count: Mapped[int | None] = mapped_column(Integer)
    core_region_count: Mapped[int | None] = mapped_column(Integer)
    core_scenario_average_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    core_scenarios_valued: Mapped[int | None] = mapped_column(Integer)
    no_profitable_region: Mapped[bool | None] = mapped_column(Boolean)
    profit_concentrated_in_single_region: Mapped[bool | None] = mapped_column(Boolean)

    # --- TAIL STRESS (Sections 15-17) -- kept strictly separate; these
    # never enter a core statistic or the ranking key.
    stress_worst_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    stress_large_move_survival: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    stress_vs_core_worst_delta: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    stress_scenarios_valued: Mapped[int | None] = mapped_column(Integer)

    # --- EXECUTION QUALITY (Section 22) ---
    mean_relative_spread: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    worst_relative_spread: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    two_sided_leg_count: Mapped[int | None] = mapped_column(Integer)
    leg_count: Mapped[int | None] = mapped_column(Integer)
    required_sides_complete: Mapped[bool | None] = mapped_column(Boolean)
    max_leg_timestamp_skew_seconds: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    earliest_leg_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_leg_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    market_data_quality: Mapped[str | None] = mapped_column(String(48))

    # --- CAPITAL (Section 23) ---
    standardized_capital: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    entry_cash_required: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    capital_utilisation: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    # --- RANK PROVENANCE (Sections 76, 77) -- everything affecting rank
    # is serialized; there is no process-local hidden feature and no
    # random tie-break.
    #: V4 consolidation (Section 26): the full per-scenario T+1 surface,
    #: CORE and TAIL STRESS kept as separate lists. Persisted so the
    #: scenario matrix can be rendered from frozen evidence rather than
    #: recomputed -- a recomputation could silently drift from what was
    #: actually ranked. Shape: {"core": [...], "stress": [...]} with each
    #: cell {scenario_id, move_label, em_fraction, iv_label, iv_multiplier,
    #: return_executable, return_theoretical}.
    scenario_grid: Mapped[dict | None] = mapped_column(JSON)
    ranking_key: Mapped[dict | None] = mapped_column(JSON)
    rank_explanation: Mapped[str | None] = mapped_column(Text)
    data_quality_warnings: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    decision: Mapped[V4ShadowDecision] = relationship(back_populates="candidates")
    legs: Mapped[list[V4ShadowCandidateLeg]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "shadow_decision_id", "candidate_id", name="uq_v4_shadow_candidate_identity"
        ),
    )


class V4ShadowCandidateLeg(Base):
    """Per-leg point-in-time execution evidence (Sections 20, 75).

    ``retrieved_at`` is genuinely per leg -- never one aggregate stamp
    copied across legs, which is what made cross-leg skew unmeasurable
    before this migration's own predecessor fixed it in the provider."""

    __tablename__ = "v4_shadow_candidate_leg"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shadow_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_candidate.id"), nullable=False, index=True
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)

    action: Mapped[str] = mapped_column(String(8), nullable=False)
    right: Mapped[str] = mapped_column(String(8), nullable=False)
    strike: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    external_contract_id: Mapped[str | None] = mapped_column(String(32))

    #: "ask" for a buy, "bid" for a sell -- the one authoritative entry
    #: rule. Stored explicitly so a reader never has to re-derive it.
    required_side: Mapped[str | None] = mapped_column(String(8))
    required_side_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    implied_volatility: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    delta: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    gamma: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    theta: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    vega: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    volume: Mapped[int | None] = mapped_column(Integer)
    open_interest: Mapped[int | None] = mapped_column(Integer)

    market_data_quality: Mapped[str | None] = mapped_column(String(24))
    source_provider: Mapped[str | None] = mapped_column(String(64))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quote_age_seconds: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    candidate: Mapped[V4ShadowCandidate] = relationship(back_populates="legs")

    __table_args__ = (
        UniqueConstraint("shadow_candidate_id", "leg_index", name="uq_v4_shadow_leg_index"),
    )


class V4ShadowObservation(Base):
    """An observation of real executable quotes at a legal window --
    ENTRY or EXIT (Sections 20, 26, 27, 78).

    Deliberately NOT called a fill, an order, or a position. No brokerage
    order is ever submitted; this records what the market was actually
    showing on the required side at the moment the window opened."""

    __tablename__ = "v4_shadow_observation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shadow_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), nullable=False, index=True
    )
    #: "ENTRY" | "EXIT"
    phase: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: "OBSERVED" | "NOT_EXECUTABLE" | "FAILED"
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    failure_category: Mapped[str | None] = mapped_column(String(48))
    failure_detail: Mapped[str | None] = mapped_column(Text)

    #: Net cash on the executable convention. Entry: what it would have
    #: cost. Exit: what it would have realized. Never a midpoint, never a
    #: last-price fallback, never a theoretical expiration value.
    net_executable_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    market_data_quality: Mapped[str | None] = mapped_column(String(48))
    source_provider: Mapped[str | None] = mapped_column(String(64))
    max_leg_timestamp_skew_seconds: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    legs_json: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "shadow_decision_id", "phase", name="uq_v4_shadow_observation_one_per_phase"
        ),
    )


class V4ShadowSettlement(Base):
    """Realized T+1 outcome -- the ONLY place outcome data lives
    (Section 63). Written at settlement time, long after the decision and
    its candidates were frozen, so decision-time code cannot reach it."""

    __tablename__ = "v4_shadow_settlement"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shadow_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), nullable=False, unique=True, index=True
    )
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: "SETTLED" | "OBSERVATION_FAILED"
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    failure_category: Mapped[str | None] = mapped_column(String(48))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    #: Timing policy the EXIT was observed under (migration e3a5c7d9b1f2). May
    #: differ from the decision's own version: an entry frozen under v1 (15:55
    #: settlement) settled prospectively under v2 (15:30) records v2 here and keeps
    #: v1 on the immutable decision/entry rows.
    timing_policy_version: Mapped[str | None] = mapped_column(String(64))

    entry_net_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    exit_net_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    return_on_standardized_capital: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    market_data_quality: Mapped[str | None] = mapped_column(String(48))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class V4ShadowRunEvent(Base):
    """Append-only attempt/failure history (Sections 8, 33, 53).

    Immutability means a frozen decision is never edited -- so retry and
    failure information is appended here instead. A V4 failure recorded
    on this table has, by construction, no effect on any official V3
    row."""

    __tablename__ = "v4_shadow_run_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Nullable: a failure can occur before any decision row exists.
    shadow_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), index=True
    )
    earnings_calendar_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("earnings_calendar_event.id"), index=True
    )
    ticker: Mapped[str | None] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Pipeline stage, e.g. "view", "candidates", "valuation", "ranking",
    #: "entry_observation", "settlement_observation".
    stage: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    #: One of SHADOW_FAILURE_CATEGORIES, or "OK".
    category: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class V4ShadowConfigResult(Base):
    """One of the six V4 configurations' frozen result over a shared
    event-level evidence freeze (V4 product consolidation, Sections 48-51).

    WHY THIS IS A SEPARATE TABLE
    ----------------------------
    ``V4ShadowDecision`` already holds exactly the right EVENT-LEVEL
    evidence: the DecisionView and its LLM provenance, the underlying
    observation, market-data quality, every methodology version stamp, and
    the latency/request-budget measurements. None of that varies by capital
    or risk profile, and duplicating it six times would be both wasteful and
    actively misleading -- six copies could drift, and a reader could no
    longer prove the six configurations saw the same market.

    So the decision row stays the single evidence freeze, the candidate rows
    stay the single shared candidate universe, and THIS table holds only
    what genuinely differs per configuration: which candidates were
    eligible, how they ranked, and which one (if any) came first.

    Append-only, like every other shadow table -- protected by the same
    database-level ``reject_snapshot_update()`` BEFORE UPDATE trigger.
    """

    __tablename__ = "v4_shadow_config_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shadow_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), nullable=False, index=True
    )

    # --- configuration identity, stored explicitly (Section 50) ------------
    #: Never inferred later from candidate text or a label string.
    configuration_key: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    capital_base: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    risk_profile: Mapped[str] = mapped_column(String(24), nullable=False)
    configuration_version: Mapped[str] = mapped_column(String(64), nullable=False)
    max_risk_dollars: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    max_risk_utilization_pct: Mapped[Decimal] = mapped_column(Numeric(9, 4), nullable=False)

    # --- this configuration's own outcome ---------------------------------
    #: "RANKED" or "NO_ACTION". NO_ACTION is a real result, not a failure
    #: (Section 17) -- no rule is relaxed to make all six trade.
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    no_action_reason: Mapped[str | None] = mapped_column(Text)
    rank_1_candidate_id: Mapped[str | None] = mapped_column(String(128))
    eligible_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Per-candidate exclusion reasons, as
    #: ``[{"candidate_id": ..., "reason_code": ..., "detail": ...}]``.
    #: Kept so the UI can answer "why was this not considered for $2K
    #: Conservative?" without recomputing anything.
    exclusions: Mapped[list | None] = mapped_column(JSON)

    #: The ordered candidate ids as this configuration ranked them.
    #: References the SHARED candidate rows -- never a private copy.
    ranked_candidate_ids: Mapped[list | None] = mapped_column(JSON)

    ranking_version: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    decision: Mapped[V4ShadowDecision] = relationship(back_populates="config_results")

    __table_args__ = (
        # Exactly one result per configuration per decision (Section 51).
        UniqueConstraint(
            "shadow_decision_id",
            "configuration_key",
            name="uq_v4_shadow_config_result_decision_configuration",
        ),
    )


# ---------------------------------------------------------------------------
# Six-cohort forward evidence (V4 activation phase, Sections 4-16).
#
# ONE candidate-level MARKET observation per unique selected candidate and
# phase (shared by every configuration that selected that candidate), plus
# CONFIGURATION-level position and settlement records that freeze the
# per-configuration quantity, capital and realized result. Quote evidence is
# therefore stored once per unique candidate, never once per configuration.
# All three are append-only under the same reject_snapshot_update() trigger.
# ---------------------------------------------------------------------------


class V4ShadowCandidateObservation(Base):
    """Executable quote observation for ONE frozen candidate at ONE phase.

    ENTRY: buy legs at ASK, sell legs at BID (the frozen entry quotes).
    EXIT:  close longs at BID, close shorts at ASK (re-quoted by conId).
    ``net_executable_value`` is for ONE unit of the structure; quantity is
    a configuration concern and lives on the configuration rows.
    """

    __tablename__ = "v4_shadow_candidate_observation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shadow_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), nullable=False, index=True
    )
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(8), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    failure_category: Mapped[str | None] = mapped_column(String(48))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    net_executable_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    market_data_quality: Mapped[str | None] = mapped_column(String(24))
    source_provider: Mapped[str | None] = mapped_column(String(64))
    earliest_leg_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_leg_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_leg_timestamp_skew_seconds: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    unique_contract_count: Mapped[int | None] = mapped_column(Integer)
    legs_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "shadow_decision_id",
            "candidate_id",
            "phase",
            name="uq_v4_shadow_candidate_observation_one_per_candidate_phase",
        ),
    )


class V4ShadowConfigEntry(Base):
    """A configuration's frozen POSITION at entry: which candidate, how many
    contracts, capital used, max risk, and the entry value at the shared
    candidate observation's executable prices. One per configuration result
    (idempotent by construction); NO_ACTION configurations get none."""

    __tablename__ = "v4_shadow_config_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shadow_config_result_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_config_result.id"), nullable=False, unique=True
    )
    shadow_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), nullable=False, index=True
    )
    candidate_observation_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_candidate_observation.id"), nullable=False, index=True
    )
    configuration_key: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
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
    timing_policy_version: Mapped[str | None] = mapped_column(String(64))
    engine_version: Mapped[str | None] = mapped_column(String(48))
    configuration_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class V4ShadowConfigSettlement(Base):
    """A configuration's realized T+1 result for its frozen position, at the
    shared EXIT candidate observation's executable prices, times its own
    quantity. Only configurations with an OBSERVED entry are ever settled.

    A configuration may carry more than one settlement ATTEMPT -- a failed
    attempt is immutable and is never rewritten, so a later end-of-day
    recovery is appended as a new row that supersedes it. At most one of a
    configuration's attempts may be SETTLED, which the partial unique index
    below enforces in the database rather than by convention."""

    __tablename__ = "v4_shadow_config_settlement"
    __table_args__ = (
        Index(
            "uq_v4_shadow_config_settlement_one_settled_per_config",
            "shadow_config_result_id",
            unique=True,
            postgresql_where=text("status = 'SETTLED'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shadow_config_result_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_config_result.id"), nullable=False, index=True
    )
    shadow_decision_id: Mapped[int] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), nullable=False, index=True
    )
    candidate_observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("v4_shadow_candidate_observation.id"), index=True
    )
    configuration_key: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    standardized_capital: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    capital_used: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    entry_net_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    exit_net_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    return_on_standardized_capital: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    entry_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pricing_convention: Mapped[str] = mapped_column(String(48), nullable=False)
    market_data_quality: Mapped[str | None] = mapped_column(String(24))
    failure_category: Mapped[str | None] = mapped_column(String(48))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    #: Timing policy the EXIT was observed under (migration e3a5c7d9b1f2). May
    #: differ from the decision's own version: an entry frozen under v1 (15:55
    #: settlement) settled prospectively under v2 (15:30) records v2 here and keeps
    #: v1 on the immutable decision/entry rows.
    timing_policy_version: Mapped[str | None] = mapped_column(String(64))
    # Explicit end-of-day settlement provenance (2026-09-04). pricing_method
    # carries the real pricing-source labels this settlement used
    # (EXECUTABLE_BID/EXECUTABLE_ASK/MARKET_CLOSE_FALLBACK/
    # EXPIRATION_INTRINSIC_AT_CLOSE); supersedes_settlement_id points a
    # recovery attempt back at the failed attempt it replaces, which is
    # retained unaltered.
    pricing_method: Mapped[str | None] = mapped_column(String(96))
    recovery_provenance: Mapped[str | None] = mapped_column(String(48))
    supersedes_settlement_id: Mapped[int | None] = mapped_column(
        ForeignKey("v4_shadow_config_settlement.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class V4ForwardWindowTelemetry(Base):
    """Timing evidence for the 15:30 ET forward window (settlement-priority
    hardening, v4.0.0). One row per settlement attempt (``phase =
    "settlement"``, with the position) and one summary row per phase
    (``shadow_decision_id`` NULL). Operational telemetry, append-only by
    convention; it is not decision evidence and is never read by the engine."""

    __tablename__ = "v4_forward_window_telemetry"

    id: Mapped[int] = mapped_column(primary_key=True)
    phase: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    scheduler_run_id: Mapped[int | None] = mapped_column(ForeignKey("scheduler_run.id"), index=True)
    shadow_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("v4_shadow_decision.id"), index=True
    )
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    market_data_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    market_data_acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_contract_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    required_side_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_wait_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
