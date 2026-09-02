"""V4.4C -- V4 shadow decision generation and evidence freeze.

WHAT THIS IS. At the same legal decision window V3 uses, and from the
same point-in-time market state, this produces an immutable record of
what V4 *would* have chosen and why -- alongside V3, never instead of it.
V3 remains the official engine; nothing here writes to a V3 table, and
nothing here can place, simulate, or imply a brokerage order.

WHY IT EXISTS. V4.4B's historical replay could not answer the questions
that matter, because the point-in-time inputs were never frozen: the V4
market view was absent (so V4.2 semantics were inert), and only V3's own
chosen candidate survived (so no competing set existed to rank against).
Reconstruction after the fact cannot fix that honestly. Forward shadow
evidence can.

THE PIPELINE (Section 14), explicit and in order:

    DecisionView  ->  V4.2 semantic interpretation
                  ->  expected-move context
                  ->  complete listed-strike metadata
                  ->  bounded V4.3.1 candidate geometries
                  ->  dedupe exact contracts
                  ->  quote ONLY required contracts
                  ->  V4.4A T+1 core valuation (+ V4.4C tail stress)
                  ->  V4.4B rank
                  ->  freeze the COMPLETE candidate set
                  ->  freeze rank #1 as the shadow recommendation

No full-chain market-data sweep at any point.

FAILURE ISOLATION (Sections 32, 33). Every entry point here returns a
result object and raises nothing at its boundary. A V4 failure is
recorded as shadow evidence and must never propagate into, delay, or
fail the official V3 path.

NO LOOK-AHEAD (Sections 7, 44, 63). This module imports no settlement,
exit, price-reaction, or realized-outcome data of any kind -- asserted
structurally in tests/test_v4_4c_shadow_isolation.py. Realized outcome
exists only on V4ShadowSettlement, written much later.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from analytics.decision.v4_4b_ranking import (
    RANKING_VERSION,
    RankableCandidate,
    RankedCandidate,
    explain_pairwise,
    rank_candidates,
)
from analytics.decision.v4_capital import PER_DECISION_CAPITAL
from analytics.decision.v4_compatibility import (
    COMPATIBILITY_VERSION,
    SemanticCompatibilityResult,
)
from analytics.decision.v4_configurations import V4_CONFIGURATIONS
from analytics.decision.v4_methodology import V4_METHODOLOGY
from analytics.decision.v4_t1_pricing import (
    evaluate_candidate_t1_scenarios,
    summarize_candidate_distribution,
)
from analytics.decision.v4_t1_stress_grid import (
    SCENARIO_GRID_VERSION,
    TailStressDiagnostic,
    build_stress_scenarios,
    summarize_tail_stress,
)
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.v4_shadow import (
    SHADOW_SCHEMA_VERSION,
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowConfigResult,
    V4ShadowDecision,
    V4ShadowObservation,
    V4ShadowRunEvent,
)
from services.v4_config_evaluation import evaluate_configuration
from services.v4_shadow_cohort import freeze_config_entries

#: Section 35 -- a hard safety cap. V4.3.1 already targets tens, not
#: hundreds, but a runaway generator must never be able to blow the
#: latency budget silently. Truncation is always LOGGED as a run event,
#: never silent.
log = logging.getLogger("services.v4_shadow")

MAX_CANDIDATES = 60

#: Section 4/73 -- versioned so a stored view can be interpreted years
#: later without guessing which fields were captured.
DECISION_VIEW_SCHEMA_VERSION = "v4-decision-view-v1"


@dataclass(frozen=True)
class ShadowDecisionView:
    """The frozen structured market view -- the primary gap V4.4B's
    replay exposed. Persisted as data, never as prose alone."""

    direction: str | None
    volatility_view: str | None
    expected_move_intent: str | None
    confidence: str | None
    reasoning: str | None
    evidence_refs: dict | None
    llm_provider: str | None
    llm_model: str | None
    prompt_version: str | None
    schema_version: str = DECISION_VIEW_SCHEMA_VERSION


@dataclass
class ShadowCandidateInput:
    """One fully-resolved candidate, ready to value and rank. Built by the
    caller from real point-in-time quotes -- this module never fetches
    market data itself, which keeps the request budget owned by one
    place (Section 13/36)."""

    candidate_id: str
    context: V4T1ValuationContext
    semantic_compatibility: SemanticCompatibilityResult | None = None
    geometry_variant_id: str | None = None
    external_contract_ids: dict[int, str] = field(default_factory=dict)
    leg_retrieved_at: dict[int, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class ShadowGenerationResult:
    #: ALREADY_GENERATED is an idempotency outcome, NOT a failure -- a
    #: scheduler retry for a window that already has a frozen decision is
    #: correct behaviour, and must not be counted or alerted as an error
    #: (same distinction Section 54 draws for NO_ACTION).
    status: Literal["RANKED", "NO_ACTION", "FAILED", "ALREADY_GENERATED"]
    decision_id: int | None
    rank_1_candidate_id: str | None
    candidate_count: int
    rankable_count: int
    reason: str | None
    failure_category: str | None
    latency_ms: Decimal


def _entry_cash(legs: tuple[V4T1LegInput, ...]) -> Decimal | None:
    """Executable entry cost: BUY pays ASK, SELL receives BID. Never a
    midpoint, never a last-price fallback (Section 26)."""
    total = Decimal(0)
    for leg in legs:
        price = leg.entry_executable_price
        if price is None:
            return None
        sign = Decimal(1) if leg.action == "buy" else Decimal(-1)
        total += sign * price * Decimal(leg.quantity) * leg.multiplier
    return total


def _leg_skew_seconds(retrieved: dict[int, datetime]) -> Decimal | None:
    """Section 75 -- real cross-leg skew from genuinely per-leg
    timestamps, never one aggregate stamp copied across legs."""
    stamps = [t for t in retrieved.values() if t is not None]
    if len(stamps) < 2:
        return Decimal(0) if stamps else None
    return Decimal(str((max(stamps) - min(stamps)).total_seconds()))


def evaluate_shadow_candidate(
    candidate: ShadowCandidateInput,
) -> tuple[RankableCandidate, TailStressDiagnostic]:
    """Core valuation (V4.4A, unchanged) plus V4.4C tail stress, kept
    strictly separate. The stress points never enter the core statistics
    the ranker consumes -- see v4_t1_stress_grid's own docstring for why
    mixing them would be a silent methodology change."""
    core = evaluate_candidate_t1_scenarios(candidate.context, candidate.candidate_id) or ()
    distribution = summarize_candidate_distribution(core) if core else None

    # Stress: same pricing machinery, separate grid, separate summary.
    stress_returns: list[Decimal | None] = []
    stress_results: list = []
    stress_scenarios = build_stress_scenarios(candidate.context.expected_move_context)
    if stress_scenarios:
        from analytics.decision.v4_t1_pricing import evaluate_candidate_t1_scenario  # noqa: PLC0415
        from analytics.decision.v4_t1_scenario_grid import build_iv_scenarios  # noqa: PLC0415

        for underlying in stress_scenarios:
            for iv in build_iv_scenarios():
                result = evaluate_candidate_t1_scenario(
                    candidate.context, underlying, iv, "NORMAL_FRICTION", candidate.candidate_id
                )
                stress_returns.append(result.return_on_standardized_capital_executable)
                stress_results.append(result)

    stress = summarize_tail_stress(
        stress_returns, distribution.worst_scenario_return if distribution else None
    )
    # Attach the raw cells for evidence persistence (Section 26). replace()
    # keeps the diagnostic frozen; the ranker never reads this field.
    stress = dataclasses.replace(stress, results=tuple(stress_results))

    cash = _entry_cash(candidate.context.legs)
    rankable = RankableCandidate(
        candidate_id=candidate.candidate_id,
        context=candidate.context,
        scenario_results=core,
        distribution=distribution,
        semantic_compatibility=candidate.semantic_compatibility,
        entry_cash_required=cash,
        capital_utilisation=(abs(cash) / PER_DECISION_CAPITAL if cash is not None else None),
        max_leg_timestamp_skew_seconds=_leg_skew_seconds(candidate.leg_retrieved_at),
    )
    return rankable, stress


def _scenario_cell(r) -> dict:
    return {
        "scenario_id": r.scenario_id,
        "move_label": r.underlying_move_label,
        "em_fraction": str(r.underlying_move_em_fraction),
        "scenario_underlying_price": str(r.scenario_underlying_price),
        "iv_label": r.iv_scenario_label,
        "iv_multiplier": str(r.iv_scenario_multiplier),
        "return_executable": (
            None
            if r.return_on_standardized_capital_executable is None
            else str(r.return_on_standardized_capital_executable)
        ),
        "return_theoretical": (
            None
            if r.return_on_standardized_capital_theoretical is None
            else str(r.return_on_standardized_capital_theoretical)
        ),
        "reason_codes": list(r.reason_codes),
    }


def _expected_move_payload(ctx) -> dict | None:
    if ctx is None:
        return None

    def s(v):
        return None if v is None else str(v)

    return {
        "spot": s(ctx.spot),
        "observed_at": ctx.observed_at.isoformat() if ctx.observed_at else None,
        "implied_move_available": ctx.implied_move_available,
        "implied_move_dollars": s(ctx.implied_move_dollars),
        "implied_move_pct": s(ctx.implied_move_pct),
        "upper_implied_boundary": s(ctx.upper_implied_boundary),
        "lower_implied_boundary": s(ctx.lower_implied_boundary),
        "implied_move_source": ctx.implied_move_source,
        "historical_sample_n": ctx.historical_sample_n,
        "historical_evidence_quality": ctx.historical_evidence_quality,
        "historical_median_abs_move_pct": s(ctx.historical_median_abs_move_pct),
        "historical_median_upper_boundary": s(ctx.historical_median_upper_boundary),
        "historical_median_lower_boundary": s(ctx.historical_median_lower_boundary),
        "context_version": ctx.context_version,
    }


def _persist_candidate(
    db: Session,
    decision: V4ShadowDecision,
    ranked: RankedCandidate,
    source: ShadowCandidateInput,
    stress: TailStressDiagnostic,
    rank_explanation: str | None,
    rankable: RankableCandidate | None = None,
) -> None:
    stamps = [t for t in source.leg_retrieved_at.values() if t is not None]
    row = V4ShadowCandidate(
        shadow_decision_id=decision.id,
        candidate_id=ranked.candidate_id,
        strategy=ranked.strategy,
        expiration=source.context.expiration,
        geometry_variant_id=source.geometry_variant_id,
        rank=ranked.rank,
        validity_status=ranked.status,
        status_reason=ranked.status_reason,
        semantic_compatibility=ranked.semantic_compatibility,
        semantic_tier=ranked.semantic_tier,
        semantic_reason_codes=(
            {"codes": list(source.semantic_compatibility.reason_codes)}
            if source.semantic_compatibility is not None
            else None
        ),
        core_worst_return=ranked.worst_executable_return,
        core_median_return=ranked.median_executable_return,
        core_best_return=ranked.best_executable_return,
        core_positive_scenario_fraction=ranked.positive_scenario_fraction,
        core_positive_region_count=ranked.robustness.n_positive_underlying_regions,
        core_region_count=ranked.robustness.n_underlying_regions,
        core_scenario_average_return=ranked.scenario_average_return,
        core_scenarios_valued=ranked.n_scenarios_valued,
        no_profitable_region=ranked.robustness.no_profitable_region,
        profit_concentrated_in_single_region=(
            ranked.robustness.profit_concentrated_in_single_region
        ),
        stress_worst_return=stress.stress_worst_return,
        stress_large_move_survival=stress.stress_large_move_survival,
        stress_vs_core_worst_delta=stress.stress_vs_core_worst_delta,
        stress_scenarios_valued=stress.n_stress_valued,
        mean_relative_spread=ranked.mean_relative_spread,
        worst_relative_spread=ranked.execution.worst_relative_spread,
        two_sided_leg_count=ranked.execution.n_legs_with_two_sided_quote,
        leg_count=ranked.execution.n_legs,
        required_sides_complete=ranked.execution.all_required_sides_present,
        max_leg_timestamp_skew_seconds=source.leg_retrieved_at
        and _leg_skew_seconds(source.leg_retrieved_at),
        earliest_leg_observed_at=min(stamps) if stamps else None,
        latest_leg_observed_at=max(stamps) if stamps else None,
        market_data_quality=ranked.market_data_quality,
        standardized_capital=PER_DECISION_CAPITAL,
        entry_cash_required=ranked.entry_cash_required,
        capital_utilisation=ranked.capital_utilisation,
        scenario_grid=(
            {
                "core": [_scenario_cell(r) for r in rankable.scenario_results],
                "stress": [_scenario_cell(r) for r in getattr(stress, "results", ())],
            }
            if rankable is not None
            else None
        ),
        ranking_key={"key": list(ranked.ranking_key)} if ranked.ranking_key else None,
        rank_explanation=rank_explanation or ranked.rationale,
        data_quality_warnings={"warnings": list(ranked.data_quality_warnings)},
    )
    db.add(row)
    db.flush()

    for leg in source.context.legs:
        db.add(
            V4ShadowCandidateLeg(
                shadow_candidate_id=row.id,
                leg_index=leg.leg_index,
                action=leg.action,
                right=leg.right,
                strike=leg.strike,
                quantity=leg.quantity,
                multiplier=leg.multiplier,
                external_contract_id=source.external_contract_ids.get(leg.leg_index)
                or leg.external_contract_id,
                required_side="ask" if leg.action == "buy" else "bid",
                required_side_price=leg.entry_executable_price,
                bid=leg.entry_bid,
                ask=leg.entry_ask,
                last_price=leg.entry_last,
                implied_volatility=leg.entry_iv,
                delta=leg.entry_delta,
                gamma=leg.entry_gamma,
                theta=leg.entry_theta,
                vega=leg.entry_vega,
                market_data_quality=leg.market_data_quality,
                source_provider="ibkr_tws",
                retrieved_at=source.leg_retrieved_at.get(leg.leg_index),
            )
        )


def generate_shadow_decision(
    db: Session,
    *,
    earnings_calendar_event_id: int,
    ticker: str,
    company_name: str,
    legal_decision_window_at: datetime,
    as_of: datetime,
    view: ShadowDecisionView,
    candidates: list[ShadowCandidateInput],
    underlying_price: Decimal | None = None,
    underlying_quote_at: datetime | None = None,
    market_data_quality: str | None = None,
    tws_request_count: int | None = None,
    unique_contracts_quoted: int | None = None,
) -> ShadowGenerationResult:
    """Freezes one immutable shadow decision plus its COMPLETE candidate
    set. Never raises at this boundary (Section 33): a failure becomes
    recorded shadow evidence, so the official V3 path is unaffected."""
    started = time.monotonic()
    now = datetime.now(UTC)

    # Section 33 -- failure isolation, done with a SAVEPOINT rather than a
    # plain session rollback. A bare db.rollback() in the handler below
    # would unwind the ENTIRE surrounding transaction, which in the
    # scheduler is shared with whatever else that run has already done --
    # so a V4 shadow bug could silently discard unrelated (potentially
    # official) work. That is exactly the coupling this phase exists to
    # prevent. A nested transaction confines the blast radius to the
    # shadow writes alone, leaving the caller's transaction intact.
    # Section 47 -- idempotency, checked cheaply up front. The unique
    # constraint is still the real guarantee (and is caught below as a
    # race safety net), but a retry should not redo the whole valuation
    # just to be rejected by the database.
    existing = (
        db.query(V4ShadowDecision)
        .filter_by(
            earnings_calendar_event_id=earnings_calendar_event_id,
            legal_decision_window_at=legal_decision_window_at,
            engine_version=V4_METHODOLOGY.engine_version,
        )
        .one_or_none()
    )
    if existing is not None:
        return ShadowGenerationResult(
            status="ALREADY_GENERATED",
            decision_id=existing.id,
            rank_1_candidate_id=existing.rank_1_candidate_id,
            candidate_count=existing.candidate_count,
            rankable_count=existing.rankable_candidate_count,
            reason="a shadow decision is already frozen for this event/window/engine",
            failure_category=None,
            latency_ms=Decimal(str((time.monotonic() - started) * 1000)),
        )

    savepoint = db.begin_nested()
    try:
        truncated = False
        if len(candidates) > MAX_CANDIDATES:
            # Section 35 -- never silent.
            truncated = True
            candidates = candidates[:MAX_CANDIDATES]

        evaluated: list[tuple[ShadowCandidateInput, RankableCandidate, TailStressDiagnostic]] = []
        for candidate in candidates:
            rankable, stress = evaluate_shadow_candidate(candidate)
            evaluated.append((candidate, rankable, stress))

        ranked_rows = rank_candidates([r for _, r, _ in evaluated])
        by_id = {r.candidate_id: r for r in ranked_rows}
        rankable_rows = [r for r in ranked_rows if r.rank is not None]

        # Section 25 -- NO_ACTION is a legitimate OUTCOME, never a forced
        # trade to pad the benchmark sample.
        if rankable_rows:
            status: Literal["RANKED", "NO_ACTION", "FAILED"] = "RANKED"
            rank_1 = min(rankable_rows, key=lambda r: r.rank or 10**6).candidate_id
            no_action_reason = None
        else:
            status = "NO_ACTION"
            rank_1 = None
            reasons: list[str] = sorted({str(r.status) for r in ranked_rows})
            no_action_reason = (
                "no candidate was honestly rankable: " + ", ".join(reasons)
                if reasons
                else "no candidate could be constructed from point-in-time evidence"
            )

        skews = [
            s
            for s in (_leg_skew_seconds(c.leg_retrieved_at) for c, _, _ in evaluated)
            if s is not None
        ]

        decision = V4ShadowDecision(
            earnings_calendar_event_id=earnings_calendar_event_id,
            ticker=ticker,
            company_name=company_name,
            legal_decision_window_at=legal_decision_window_at,
            generated_at=now,
            as_of=as_of,
            max_input_skew_seconds=max(skews) if skews else None,
            status=status,
            no_action_reason=no_action_reason,
            view_direction=view.direction,
            view_volatility=view.volatility_view,
            view_expected_move_intent=view.expected_move_intent,
            view_confidence=view.confidence,
            view_reasoning=view.reasoning,
            view_evidence_refs=view.evidence_refs,
            llm_provider=view.llm_provider,
            llm_model=view.llm_model,
            prompt_version=view.prompt_version,
            decision_view_schema_version=view.schema_version,
            underlying_price=underlying_price,
            underlying_quote_at=underlying_quote_at,
            market_data_quality=market_data_quality,
            source_provider="ibkr_tws",
            expected_move=_expected_move_payload(
                candidates[0].context.expected_move_context if candidates else None
            ),
            engine_version=V4_METHODOLOGY.engine_version,
            # Section 23 -- which clock this observation ran under.
            decision_timing_policy_version=V4_TIMING_POLICY.version,
            shadow_schema_version=SHADOW_SCHEMA_VERSION,
            strategy_semantics_version=V4_METHODOLOGY.strategy_semantics_version,
            compatibility_version=COMPATIBILITY_VERSION,
            expected_move_version=V4_METHODOLOGY.strike_engine_version,
            strike_engine_version=V4_METHODOLOGY.strike_engine_version,
            geometry_version=V4_METHODOLOGY.geometry_candidate_version,
            valuation_version=V4_METHODOLOGY.t1_valuation_version,
            scenario_grid_version=SCENARIO_GRID_VERSION,
            iv_scenario_version="iv_crush_v1",
            ranking_version=RANKING_VERSION,
            rank_1_candidate_id=rank_1,
            candidate_count=len(ranked_rows),
            rankable_candidate_count=len(rankable_rows),
            total_latency_ms=Decimal(str((time.monotonic() - started) * 1000)),
            tws_request_count=tws_request_count,
            unique_contracts_quoted=unique_contracts_quoted,
        )
        db.add(decision)
        db.flush()

        # Section 76 -- a deterministic, persisted explanation of why #1
        # outranked #2, built from the ranking dimensions themselves.
        ordered = sorted(rankable_rows, key=lambda r: r.rank or 10**6)
        pairwise: dict[str, str] = {}
        if len(ordered) >= 2:
            pairwise[ordered[0].candidate_id] = explain_pairwise(ordered[0], ordered[1])

        for source, _rankable, stress in evaluated:
            ranked = by_id.get(source.candidate_id)
            if ranked is None:  # pragma: no cover -- defensive
                continue
            _persist_candidate(
                db,
                decision,
                ranked,
                source,
                stress,
                pairwise.get(source.candidate_id),
                rankable=_rankable,
            )

        # ------------------------------------------------------------------
        # Six standardized configurations (V4 consolidation, Sections 3-8).
        #
        # `evaluated` IS the one frozen market-evidence universe: one
        # DecisionView, one underlying observation, one deduplicated quote
        # sweep, one T+1 valuation per candidate -- all of it already done,
        # once, above. Each configuration below is a pure in-memory filter
        # and sort over that same list. Nothing here touches a provider,
        # an LLM, or the network; a test enforces that by refusing sockets.
        #
        # The decision row's own status/rank_1 remain the UNCONSTRAINED
        # reference ranking (no capital or risk gate). The six rows are the
        # capital- and risk-aware answers, each referencing the shared
        # candidate rows persisted above by candidate_id.
        # ------------------------------------------------------------------
        shared_universe = [r for _, r, _ in evaluated]
        for configuration in V4_CONFIGURATIONS:
            try:
                outcome = evaluate_configuration(shared_universe, configuration)
                db.add(
                    V4ShadowConfigResult(
                        shadow_decision_id=decision.id,
                        configuration_key=configuration.key,
                        capital_base=configuration.capital_base,
                        risk_profile=configuration.risk_profile.value,
                        configuration_version=outcome.configuration_version,
                        max_risk_dollars=configuration.max_risk_dollars,
                        max_risk_utilization_pct=configuration.max_risk_utilization_pct,
                        status=outcome.status,
                        no_action_reason=outcome.no_action_reason,
                        rank_1_candidate_id=outcome.rank_1_candidate_id,
                        eligible_candidate_count=outcome.eligible_candidate_count,
                        excluded_candidate_count=len(outcome.exclusions),
                        exclusions=[
                            {
                                "candidate_id": e.candidate_id,
                                "reason_code": e.reason_code,
                                "detail": e.detail,
                            }
                            for e in outcome.exclusions
                        ],
                        ranked_candidate_ids=[
                            r.candidate_id
                            for r in sorted(outcome.ranked, key=lambda r: r.rank or 10**6)
                            if r.rank is not None
                        ],
                        ranking_version=outcome.ranking_version,
                    )
                )
            except Exception as exc:  # noqa: BLE001 -- Section 8: one config never kills five
                log.error(
                    "v4 configuration %s failed for decision %s",
                    configuration.key,
                    decision.id,
                    exc_info=True,
                )
                db.add(
                    V4ShadowConfigResult(
                        shadow_decision_id=decision.id,
                        configuration_key=configuration.key,
                        capital_base=configuration.capital_base,
                        risk_profile=configuration.risk_profile.value,
                        configuration_version="unknown",
                        max_risk_dollars=configuration.max_risk_dollars,
                        max_risk_utilization_pct=configuration.max_risk_utilization_pct,
                        status="FAILED",
                        no_action_reason=f"{type(exc).__name__}: {exc}",
                        eligible_candidate_count=0,
                        excluded_candidate_count=0,
                    )
                )

        # ------------------------------------------------------------------
        # Six-cohort ENTRY evidence (activation phase, Sections 5-10): one
        # candidate-level observation per unique selected candidate, one
        # position per RANKED configuration, from the SAME frozen quotes.
        # No provider call. Failures are per-candidate/per-configuration.
        # ------------------------------------------------------------------
        db.flush()
        config_rows = db.query(V4ShadowConfigResult).filter_by(shadow_decision_id=decision.id).all()
        try:
            freeze_config_entries(
                db,
                decision=decision,
                config_rows=config_rows,
                rankable_by_id={r.candidate_id: r for _, r, _ in evaluated},
                leg_retrieved_at_by_id={
                    c.candidate_id: c.leg_retrieved_at for c, _, _ in evaluated
                },
                observed_at=legal_decision_window_at,
                market_data_quality=market_data_quality,
            )
        except Exception:  # noqa: BLE001 -- never let cohort entries break the freeze
            log.error("six-cohort entry freeze failed for decision %s", decision.id, exc_info=True)

        if truncated:
            db.add(
                V4ShadowRunEvent(
                    shadow_decision_id=decision.id,
                    earnings_calendar_event_id=earnings_calendar_event_id,
                    ticker=ticker,
                    occurred_at=now,
                    stage="candidates",
                    category="OK",
                    retryable=False,
                    message=f"candidate set truncated to the MAX_CANDIDATES cap ({MAX_CANDIDATES})",
                )
            )
        db.flush()
        savepoint.commit()

        return ShadowGenerationResult(
            status=status,
            decision_id=decision.id,
            rank_1_candidate_id=rank_1,
            candidate_count=len(ranked_rows),
            rankable_count=len(rankable_rows),
            reason=no_action_reason,
            failure_category=None,
            latency_ms=Decimal(str((time.monotonic() - started) * 1000)),
        )

    except IntegrityError as exc:
        # Race safety net: another worker froze this window between the
        # check above and the flush. Idempotent, not an error.
        savepoint.rollback()
        return ShadowGenerationResult(
            status="ALREADY_GENERATED",
            decision_id=None,
            rank_1_candidate_id=None,
            candidate_count=0,
            rankable_count=0,
            reason=f"concurrent freeze for this event/window/engine: {exc.orig}",
            failure_category=None,
            latency_ms=Decimal(str((time.monotonic() - started) * 1000)),
        )

    except Exception as exc:  # noqa: BLE001 -- boundary: V4 must never break V3
        # Unwinds ONLY the shadow writes above; the caller's transaction
        # (and anything the official V3 path already did in it) survives.
        savepoint.rollback()
        db.add(
            V4ShadowRunEvent(
                earnings_calendar_event_id=earnings_calendar_event_id,
                ticker=ticker,
                occurred_at=now,
                stage="shadow_decision",
                category="INTERNAL_ERROR",
                retryable=True,
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        db.flush()
        return ShadowGenerationResult(
            status="FAILED",
            decision_id=None,
            rank_1_candidate_id=None,
            candidate_count=0,
            rankable_count=0,
            reason=str(exc),
            failure_category="INTERNAL_ERROR",
            latency_ms=Decimal(str((time.monotonic() - started) * 1000)),
        )


def record_observation(
    db: Session,
    *,
    shadow_decision_id: int,
    phase: Literal["ENTRY", "EXIT"],
    candidate_id: str,
    legs: tuple[V4T1LegInput, ...],
    observed_at: datetime,
    market_data_quality: str | None = None,
    leg_retrieved_at: dict[int, datetime] | None = None,
) -> V4ShadowObservation:
    """Freezes an executable-quote observation (Sections 26, 27, 78).

    ENTRY uses the entry convention (BUY->ASK, SELL->BID). EXIT uses its
    inverse, because closing a long means selling into the BID and
    closing a short means buying back at the ASK. No midpoint, no last
    fallback, no theoretical expiration value, and emphatically no order.
    """
    net = Decimal(0)
    missing: list[int] = []
    leg_rows = []
    for leg in legs:
        if phase == "ENTRY":
            side = "ask" if leg.action == "buy" else "bid"
            price = leg.entry_ask if leg.action == "buy" else leg.entry_bid
            sign = Decimal(1) if leg.action == "buy" else Decimal(-1)
        else:
            # Closing: a long leg is sold at BID, a short leg bought at ASK.
            side = "bid" if leg.action == "buy" else "ask"
            price = leg.entry_bid if leg.action == "buy" else leg.entry_ask
            sign = Decimal(1) if leg.action == "buy" else Decimal(-1)
        if price is None:
            missing.append(leg.leg_index)
        else:
            net += sign * price * Decimal(leg.quantity) * leg.multiplier
        leg_rows.append(
            {
                "leg_index": leg.leg_index,
                "action": leg.action,
                "right": leg.right,
                "strike": str(leg.strike),
                "required_side": side,
                "price": str(price) if price is not None else None,
                "bid": str(leg.entry_bid) if leg.entry_bid is not None else None,
                "ask": str(leg.entry_ask) if leg.entry_ask is not None else None,
                "market_data_quality": leg.market_data_quality,
            }
        )

    status = "NOT_EXECUTABLE" if missing else "OBSERVED"
    observation = V4ShadowObservation(
        shadow_decision_id=shadow_decision_id,
        phase=phase,
        candidate_id=candidate_id,
        observed_at=observed_at,
        status=status,
        failure_category=(
            ("ENTRY_OBSERVATION_FAILED" if phase == "ENTRY" else "SETTLEMENT_OBSERVATION_FAILED")
            if missing
            else None
        ),
        failure_detail=(
            f"required side missing on leg(s) {missing} -- no midpoint or last-price "
            "substitution is permitted"
            if missing
            else None
        ),
        net_executable_value=None if missing else net,
        market_data_quality=market_data_quality,
        source_provider="ibkr_tws",
        max_leg_timestamp_skew_seconds=_leg_skew_seconds(leg_retrieved_at or {}),
        legs_json={"legs": leg_rows},
    )
    db.add(observation)
    db.flush()
    return observation
