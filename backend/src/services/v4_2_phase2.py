"""V4.2 PHASE 2 -- the independent decision engine.

    ONE EVENT
        -> ONE common point-in-time evidence freeze
        -> ONE broad multi-expiry option universe
        -> SHARED deterministic valuations
        -> SIX independent configuration decisions

WHAT IS COMMON, AND WHY
-----------------------
The DecisionView, the underlying observation, the historical move
distribution and the option metadata snapshot are acquired ONCE and read six
times. The language model is not called at all here: Phase 2 reasons over the
control's already-frozen DecisionView for the same window, exactly as Phase 1
does, so a Phase-2 run costs zero LLM calls and the two challengers cannot
disagree about what the view was.

Candidate valuation is also shared. Every candidate is priced once with the
project's own T+1 machinery and each configuration reads the same frozen
numbers, so six configurations cost one valuation pass and one market-data
acquisition -- never six.

WHAT IS INDEPENDENT
-------------------
The selection. Each configuration applies its own family permission,
liquidity floor, capital base and risk cap to the shared universe and ranks
what survives. There is no event-level winner chosen first and then offered
to the six; that ordering is precisely what made Phase 1's configurations
unable to disagree.

THE CAPITAL SCREEN, DELIBERATELY NOT INHERITED
----------------------------------------------
V4.1's ranker classifies a candidate CAPITAL_INCOMPATIBLE when its entry cash
exceeds the $2,000 standardized per-decision capital, and Phase 1 reads only
the survivors. Three of the six configurations hold $10,000. Phase 2
therefore treats capital as a per-configuration question and never as a
universe-level deletion: a structure priced above $2,000 stays in the shared
universe, is refused by the configurations that cannot hold it, and is
available to the ones that can. Every OTHER validity refusal -- a missing
required side, a missing entry IV, an unbuildable scenario grid -- remains a
hard exclusion, because those are statements about whether the candidate can
be valued honestly at all.

ZERO-WRITE BY DEFAULT
---------------------
``run_independent_search`` writes nothing. Persistence is a separate,
explicitly-called step, so the dry-run path and the production path share one
engine rather than one of them being a reimplementation of the other.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from analytics.decision.v4_2_config_policy import (
    ConfigurationDecision,
    SharedCandidate,
    decide_all_configurations,
)
from analytics.decision.v4_2_phase2_methodology import PHASE_2_VERSIONS
from analytics.decision.v4_2_viability import (
    DEFAULT_POLICY,
    CandidateEconomics,
    MoveEvidence,
    ViabilityPolicy,
)
from analytics.decision.v4_4b_ranking import (
    assess_execution_quality,
    assess_robustness,
    classify_candidate_validity,
)
from analytics.decision.v4_configurations import V4_CONFIGURATIONS, V4Configuration
from models.v4_shadow import V4ShadowDecision
from services.v4_2_move_history import anchored_move_distribution_for_ticker
from services.v4_2_multi_expiry import (
    MULTI_EXPIRY_UNAVAILABLE,
    MultiExpiryResult,
    build_multi_expiry_universe,
)
from services.v4_config_evaluation import max_defined_risk
from services.v4_shadow import ShadowCandidateInput, evaluate_shadow_candidate

log = logging.getLogger("services.v4_2_phase2")

PHASE2_STATUS_ACTION = "ACTION"
PHASE2_STATUS_NO_ACTION = "NO_ACTION"
PHASE2_STATUS_FAILED = "FAILED"

#: The one V4.1 validity verdict Phase 2 does NOT treat as a universe-level
#: exclusion. See the module docstring: capital is a per-configuration
#: question here, and three configurations hold five times the capital the
#: control's screen is calibrated to.
_CONFIG_SCOPED_VALIDITY = frozenset({"CAPITAL_INCOMPATIBLE"})


@dataclass
class Phase2Stage:
    """Measured, never estimated."""

    name: str
    requests: int = 0
    contracts: int = 0
    latency_ms: Decimal = Decimal(0)

    def as_dict(self) -> dict:
        return {
            "stage": self.name,
            "requests": self.requests,
            "contracts": self.contracts,
            "latency_ms": str(self.latency_ms),
        }


@dataclass
class Phase2Telemetry:
    """The request budget, by stage. Section 47."""

    stages: list[Phase2Stage] = field(default_factory=list)
    contracts_deduplicated: int = 0
    contracts_reused_from_control: int = 0
    challenger_only_contracts: int = 0
    valuation_latency_ms: Decimal = Decimal(0)
    selection_latency_ms: Decimal = Decimal(0)
    total_latency_ms: Decimal = Decimal(0)

    @property
    def total_requests(self) -> int:
        return sum(s.requests for s in self.stages)

    @property
    def unique_contracts(self) -> int:
        return sum(s.contracts for s in self.stages if s.name == "quotes")

    def as_dict(self) -> dict:
        return {
            "stages": [s.as_dict() for s in self.stages],
            "total_requests": self.total_requests,
            "unique_contracts": self.unique_contracts,
            "contracts_deduplicated": self.contracts_deduplicated,
            "contracts_reused_from_control": self.contracts_reused_from_control,
            "challenger_only_contracts": self.challenger_only_contracts,
            "valuation_latency_ms": str(self.valuation_latency_ms),
            "selection_latency_ms": str(self.selection_latency_ms),
            "total_latency_ms": str(self.total_latency_ms),
        }


@dataclass
class Phase2Evaluation:
    """One event's independent-search result, before (or without)
    persistence."""

    ticker: str
    status: str
    universe: list[SharedCandidate] = field(default_factory=list)
    configurations: list[ConfigurationDecision] = field(default_factory=list)
    multi_expiry: MultiExpiryResult | None = None
    telemetry: Phase2Telemetry = field(default_factory=Phase2Telemetry)
    move_distribution: Any = None
    implied_move_pct: Decimal | None = None
    underlying_price: Decimal | None = None
    underlying_quote_at: datetime | None = None
    market_data_quality: str | None = None
    candidate_detail: dict[str, dict] = field(default_factory=dict)
    failure_category: str | None = None
    failure_detail: str | None = None
    methodology_version: str = PHASE_2_VERSIONS.methodology_version

    @property
    def action_count(self) -> int:
        return sum(1 for c in self.configurations if c.status == "ACTION")

    @property
    def selected_candidate_ids(self) -> set[str]:
        return {
            c.selected_candidate_id for c in self.configurations if c.selected_candidate_id
        }

    @property
    def expiries_considered(self) -> int:
        return self.multi_expiry.expiries_considered if self.multi_expiry else 0


def _s(value: object) -> str | None:
    """Decimals are stringified for JSON columns; None stays None."""
    return None if value is None else str(value)


def _economics_from_valuation(
    candidate_id: str, strategy: str, ranked_inputs: dict
) -> CandidateEconomics:
    return CandidateEconomics(
        candidate_id=candidate_id,
        strategy=strategy,
        median_return=ranked_inputs.get("median_return"),
        worst_return=ranked_inputs.get("worst_return"),
        best_return=ranked_inputs.get("best_return"),
        positive_scenario_fraction=ranked_inputs.get("positive_scenario_fraction"),
        no_profitable_region=ranked_inputs.get("no_profitable_region"),
        semantic_compatibility=ranked_inputs.get("semantic_compatibility"),
        mean_relative_spread=ranked_inputs.get("mean_relative_spread"),
    )


def _value_one(candidate: ShadowCandidateInput) -> tuple[SharedCandidate, dict]:
    """Price one candidate once, and derive both the shared economics the
    gate reads and the evidence row a reader needs to explain it."""
    rankable, stress = evaluate_shadow_candidate(candidate)
    execution = assess_execution_quality(
        rankable.context.legs, rankable.max_leg_timestamp_skew_seconds
    )
    robustness = assess_robustness(rankable.scenario_results)
    status, reason = classify_candidate_validity(rankable)

    distribution = rankable.distribution
    semantic = (
        Decimal(str(rankable.semantic_compatibility.overall_semantic_compatibility))
        if rankable.semantic_compatibility is not None
        else None
    )
    strategy = str(rankable.context.strategy)
    economics = _economics_from_valuation(
        candidate.candidate_id,
        strategy,
        {
            "median_return": distribution.median_return if distribution else None,
            "worst_return": distribution.worst_scenario_return if distribution else None,
            "best_return": distribution.max_return if distribution else None,
            "positive_scenario_fraction": robustness.positive_scenario_fraction,
            "no_profitable_region": robustness.no_profitable_region,
            "semantic_compatibility": semantic,
            "mean_relative_spread": execution.mean_relative_spread,
        },
    )

    shared = SharedCandidate(
        candidate_id=candidate.candidate_id,
        strategy=strategy,
        economics=economics,
        entry_cash_required=rankable.entry_cash_required,
        per_contract_max_risk=max_defined_risk(rankable),
        n_legs=execution.n_legs,
        n_legs_with_two_sided_quote=execution.n_legs_with_two_sided_quote,
        data_invalid_reason=(
            None if status == "RANKABLE" or status in _CONFIG_SCOPED_VALIDITY else reason
        ),
    )

    detail = {
        "candidate_id": candidate.candidate_id,
        "strategy": strategy,
        "expiration": rankable.context.expiration,
        "geometry_variant_id": candidate.geometry_variant_id,
        "validity_status": status,
        "validity_reason": reason,
        "semantic_compatibility": semantic,
        "semantic_tier": (
            rankable.semantic_compatibility.tier
            if rankable.semantic_compatibility is not None
            else None
        ),
        "core_median_return": economics.median_return,
        "core_worst_return": economics.worst_return,
        "core_best_return": economics.best_return,
        "core_positive_scenario_fraction": economics.positive_scenario_fraction,
        "no_profitable_region": economics.no_profitable_region,
        "mean_relative_spread": execution.mean_relative_spread,
        "worst_relative_spread": execution.worst_relative_spread,
        "entry_cash_required": rankable.entry_cash_required,
        "per_contract_max_risk": shared.per_contract_max_risk,
        "n_legs": execution.n_legs,
        "n_legs_with_two_sided_quote": execution.n_legs_with_two_sided_quote,
        "market_data_quality": execution.market_data_quality,
        "max_leg_timestamp_skew_seconds": rankable.max_leg_timestamp_skew_seconds,
        "tail_stress_note": getattr(stress, "note", None),
        # JSON-safe by construction: every Decimal is stringified, matching
        # the convention the entry and settlement evidence already use. A
        # Decimal reaching a JSON column raises at flush time, which is a
        # failure a forward window cannot afford.
        "legs": [
            {
                "leg_index": leg.leg_index,
                "action": leg.action,
                "right": leg.right,
                "strike": _s(leg.strike),
                "quantity": leg.quantity,
                "multiplier": _s(leg.multiplier),
                "required_side": "ask" if leg.action == "buy" else "bid",
                "bid": _s(leg.entry_bid),
                "ask": _s(leg.entry_ask),
                "implied_volatility": _s(leg.entry_iv),
                "delta": _s(leg.entry_delta),
                "bid_size": leg.entry_bid_size,
                "ask_size": leg.entry_ask_size,
                "volume": leg.entry_volume,
                "open_interest": leg.entry_open_interest,
                "external_contract_id": leg.external_contract_id,
                "market_data_quality": leg.market_data_quality,
                "expiration": rankable.context.expiration.isoformat(),
            }
            for leg in rankable.context.legs
        ],
    }
    return shared, detail


def run_independent_search(
    db: Session,
    *,
    provider: Any,
    decision: V4ShadowDecision,
    settlement_date: date,
    earnings_date: date | None = None,
    as_of: datetime | None = None,
    policy: ViabilityPolicy | None = None,
    max_variants: int = 3,
    control_contract_ids: set[str] | None = None,
) -> Phase2Evaluation:
    """Evaluate one event independently. Reads market data; writes nothing.

    Never raises at this boundary: a provider fault becomes a recorded
    Phase-2 failure, exactly as Phase 1 records one, so a challenger problem
    can never reach the control.
    """
    started = time.monotonic()
    policy = policy or DEFAULT_POLICY
    now = as_of or datetime.now(UTC)
    evaluation = Phase2Evaluation(ticker=decision.ticker, status=PHASE2_STATUS_FAILED)

    # ---- 1. common point-in-time evidence --------------------------------
    distribution = anchored_move_distribution_for_ticker(
        db, ticker=decision.ticker, as_of=decision.generated_at.date()
    )
    evaluation.move_distribution = distribution

    try:
        universe_result = build_multi_expiry_universe(
            provider=provider,
            ticker=decision.ticker,
            as_of=now,
            direction=str(decision.view_direction or "neutral"),
            volatility_view=(
                str(decision.view_volatility) if decision.view_volatility else None
            ),
            earnings_date=earnings_date or decision.generated_at.date(),
            settlement_date=settlement_date,
            historical_next_day_move_pcts=list(distribution.signed_moves) or None,
            max_variants=max_variants,
        )
    except Exception as exc:  # noqa: BLE001 -- provider boundary, never propagated
        log.error("phase-2 universe construction failed for %s", decision.ticker, exc_info=True)
        evaluation.failure_category = "UNIVERSE_CONSTRUCTION_FAILED"
        evaluation.failure_detail = f"{type(exc).__name__}: {exc}"
        evaluation.telemetry.total_latency_ms = Decimal(
            str((time.monotonic() - started) * 1000)
        )
        return evaluation

    evaluation.multi_expiry = universe_result
    evaluation.underlying_price = universe_result.underlying_price
    evaluation.underlying_quote_at = universe_result.underlying_quote_at
    evaluation.market_data_quality = universe_result.market_data_quality

    budget = universe_result.budget
    evaluation.telemetry.stages = [
        Phase2Stage("underlying", budget.underlying_quotes, 0, Decimal(0)),
        Phase2Stage("metadata", budget.metadata_calls, 0, universe_result.metadata_latency_ms),
        Phase2Stage(
            "chain_discovery",
            budget.chain_discovery_calls,
            len(universe_result.listed_strikes),
            Decimal(0),
        ),
        Phase2Stage(
            "quotes",
            budget.selected_leg_quote_calls,
            budget.unique_contracts_quoted,
            universe_result.quote_latency_ms,
        ),
    ]
    evaluation.telemetry.contracts_deduplicated = budget.contracts_deduplicated

    if universe_result.status == MULTI_EXPIRY_UNAVAILABLE and not universe_result.candidates:
        evaluation.failure_category = universe_result.failure_category or "NO_VALID_CANDIDATE"
        evaluation.failure_detail = universe_result.failure_detail
        evaluation.telemetry.total_latency_ms = Decimal(
            str((time.monotonic() - started) * 1000)
        )
        return evaluation

    # ---- 2. value every candidate ONCE -----------------------------------
    valuation_started = time.monotonic()
    shared: list[SharedCandidate] = []
    for candidate in universe_result.candidates:
        one, detail = _value_one(candidate)
        detail["expiry_ladder_position"] = universe_result.ladder_position_by_candidate.get(
            candidate.candidate_id
        )
        detail["expiry_context"] = universe_result.expiry_context_by_candidate.get(
            candidate.candidate_id, {}
        )
        shared.append(one)
        evaluation.candidate_detail[candidate.candidate_id] = detail
    evaluation.universe = shared
    evaluation.telemetry.valuation_latency_ms = Decimal(
        str((time.monotonic() - valuation_started) * 1000)
    )

    observed = {
        str(leg.get("external_contract_id"))
        for detail in evaluation.candidate_detail.values()
        for leg in detail["legs"]
        if leg.get("external_contract_id")
    }
    reused = observed & (control_contract_ids or set())
    evaluation.telemetry.contracts_reused_from_control = len(reused)
    evaluation.telemetry.challenger_only_contracts = len(observed - reused)

    # ---- 3. six independent configuration decisions ----------------------
    nearest = next(
        (s for s in universe_result.per_expiry if s.implied_move_pct is not None), None
    )
    evaluation.implied_move_pct = nearest.implied_move_pct if nearest else None
    evidence = MoveEvidence(
        implied_move_pct=evaluation.implied_move_pct, distribution=distribution
    )

    selection_started = time.monotonic()
    evaluation.configurations = decide_all_configurations(
        shared, V4_CONFIGURATIONS, evidence=evidence, policy=policy
    )
    evaluation.telemetry.selection_latency_ms = Decimal(
        str((time.monotonic() - selection_started) * 1000)
    )

    evaluation.status = (
        PHASE2_STATUS_ACTION if evaluation.action_count else PHASE2_STATUS_NO_ACTION
    )
    evaluation.telemetry.total_latency_ms = Decimal(str((time.monotonic() - started) * 1000))
    return evaluation


def summarize_phase2(evaluation: Phase2Evaluation) -> dict:
    """An operator-facing view: what was searched, and what each of the six
    decided. Deliberately reports all six rows even when they agree, so a
    reader can see that they were asked separately."""
    return {
        "ticker": evaluation.ticker,
        "status": evaluation.status,
        "methodology_version": evaluation.methodology_version,
        "versions": PHASE_2_VERSIONS.as_dict(),
        "multi_expiry_status": (
            evaluation.multi_expiry.status if evaluation.multi_expiry else None
        ),
        "expiries_considered": evaluation.expiries_considered,
        "expiries": [
            {
                "expiration": s.variant.expiration.isoformat(),
                "ladder_position": s.variant.ladder_position,
                "entry_dte": s.variant.entry_dte,
                "dte_at_settlement": s.variant.dte_at_settlement,
                "settlement_risk": s.variant.settlement_risk,
                "implied_move_pct": (
                    None if s.implied_move_pct is None else str(s.implied_move_pct)
                ),
                "implied_move_source": s.implied_move_source,
                "candidates": len(s.candidates),
                "listed_strikes": s.listed_strike_count,
                "failure": s.failure_category,
            }
            for s in (evaluation.multi_expiry.per_expiry if evaluation.multi_expiry else [])
        ],
        "candidate_universe": len(evaluation.universe),
        "distinct_selected_candidates": len(evaluation.selected_candidate_ids),
        "telemetry": evaluation.telemetry.as_dict(),
        "historical_sample_n": int(getattr(evaluation.move_distribution, "sample_n", 0) or 0),
        "implied_move_pct": (
            None if evaluation.implied_move_pct is None else str(evaluation.implied_move_pct)
        ),
        "configurations": [
            {
                "configuration_key": c.configuration.key,
                "label": c.configuration.label,
                "capital_base": str(c.configuration.capital_base),
                "risk_profile": c.configuration.risk_profile.value,
                "max_risk_dollars": str(c.configuration.max_risk_dollars),
                "min_bid_ask_coverage": (
                    None
                    if c.configuration.min_bid_ask_coverage is None
                    else str(c.configuration.min_bid_ask_coverage)
                ),
                "status": c.status,
                "selected_candidate_id": c.selected_candidate_id,
                "strategy": _strategy_of(evaluation, c.selected_candidate_id),
                "expiration": _expiration_of(evaluation, c.selected_candidate_id),
                "quantity": c.position.quantity if c.position else None,
                "capital_used": str(c.position.capital_used) if c.position else None,
                "max_risk_used": str(c.position.max_risk_used) if c.position else None,
                "rank": c.rank,
                "reason": c.no_action_reason or c.selection_explanation,
                "diagnostics": c.diagnostics.as_dict(),
            }
            for c in evaluation.configurations
        ],
        "failure_category": evaluation.failure_category,
        "failure_detail": evaluation.failure_detail,
    }


def _strategy_of(evaluation: Phase2Evaluation, candidate_id: str | None) -> str | None:
    if candidate_id is None:
        return None
    detail = evaluation.candidate_detail.get(candidate_id)
    return detail["strategy"] if detail else None


def _expiration_of(evaluation: Phase2Evaluation, candidate_id: str | None) -> str | None:
    if candidate_id is None:
        return None
    detail = evaluation.candidate_detail.get(candidate_id)
    expiration = detail["expiration"] if detail else None
    return expiration.isoformat() if expiration is not None else None


def configuration_by_key(key: str) -> V4Configuration:
    return next(c for c in V4_CONFIGURATIONS if c.key == key)


__all__ = [
    "PHASE2_STATUS_ACTION",
    "PHASE2_STATUS_FAILED",
    "PHASE2_STATUS_NO_ACTION",
    "Phase2Evaluation",
    "Phase2Telemetry",
    "run_independent_search",
    "summarize_phase2",
]
