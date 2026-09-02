"""V4.4C -- READ-ONLY inspection of V4 shadow evidence (Section 40).

Every endpoint here is a GET. There is deliberately no mutation surface:
shadow decisions are written by the scheduler path alone, and there is no
"force a shadow entry" semantic anywhere (Section 82). Nothing here can
reach a brokerage order.

Registered only outside production, matching the existing guard on
api/routers/admin.py and api/routers/v4_experimental.py -- V4 remains
experimental, and its evidence is a research surface, not a product one.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import DbSession
from api.exceptions import NotFoundError
from models.v4_shadow import (
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowCandidateObservation,
    V4ShadowConfigEntry,
    V4ShadowConfigResult,
    V4ShadowConfigSettlement,
    V4ShadowDecision,
    V4ShadowObservation,
    V4ShadowSettlement,
)

router = APIRouter(prefix="/v4/shadow", tags=["v4-shadow"])

#: Repeated on every payload so a consumer cannot mistake this for
#: official V3 forward-test evidence (Section 66).
EXPERIMENTAL_NOTICE = (
    "EXPERIMENTAL SHADOW -- NOT OFFICIAL FORWARD TEST -- NO BROKERAGE ORDER. "
    "V3 remains the official engine; these records are analytical observations only."
)


def _decision_summary(row: V4ShadowDecision) -> dict:
    return {
        "id": row.id,
        "earnings_calendar_event_id": row.earnings_calendar_event_id,
        "ticker": row.ticker,
        "company_name": row.company_name,
        "legal_decision_window_at": row.legal_decision_window_at,
        "generated_at": row.generated_at,
        "as_of": row.as_of,
        "status": row.status,
        "no_action_reason": row.no_action_reason,
        "failure_category": row.failure_category,
        "rank_1_candidate_id": row.rank_1_candidate_id,
        "candidate_count": row.candidate_count,
        "rankable_candidate_count": row.rankable_candidate_count,
        "view": {
            "direction": row.view_direction,
            "volatility": row.view_volatility,
            "expected_move_intent": row.view_expected_move_intent,
            "confidence": row.view_confidence,
            "reasoning": row.view_reasoning,
        },
        "provenance": {
            "llm_provider": row.llm_provider,
            "llm_model": row.llm_model,
            "prompt_version": row.prompt_version,
            "decision_view_schema_version": row.decision_view_schema_version,
        },
        "timing_policy_version": row.decision_timing_policy_version,
        "expected_move": row.expected_move,
        "market_data": {
            "underlying_price": row.underlying_price,
            "underlying_quote_at": row.underlying_quote_at,
            "market_data_quality": row.market_data_quality,
            "source_provider": row.source_provider,
            "max_input_skew_seconds": row.max_input_skew_seconds,
        },
        "versions": {
            "engine": row.engine_version,
            "shadow_schema": row.shadow_schema_version,
            "strategy_semantics": row.strategy_semantics_version,
            "compatibility": row.compatibility_version,
            "strike_engine": row.strike_engine_version,
            "geometry": row.geometry_version,
            "valuation": row.valuation_version,
            "scenario_grid": row.scenario_grid_version,
            "iv_scenario": row.iv_scenario_version,
            "ranking": row.ranking_version,
        },
        "performance": {
            "total_latency_ms": row.total_latency_ms,
            "tws_request_count": row.tws_request_count,
            "unique_contracts_quoted": row.unique_contracts_quoted,
        },
        "notice": EXPERIMENTAL_NOTICE,
    }


@router.get("/decisions")
def list_shadow_decisions(
    db: DbSession,
    limit: int = Query(50, ge=1, le=200),
    ticker: str | None = Query(None, min_length=1, max_length=16),
) -> dict:
    """``ticker`` (V4 consolidation) scopes the list to one company so the
    company workspace can find its own latest decision without paging."""
    query = db.query(V4ShadowDecision)
    if ticker:
        query = query.filter(V4ShadowDecision.ticker == ticker.upper())
    rows = query.order_by(V4ShadowDecision.legal_decision_window_at.desc()).limit(limit).all()
    return {"notice": EXPERIMENTAL_NOTICE, "decisions": [_decision_summary(r) for r in rows]}


@router.get("/decisions/{decision_id}")
def get_shadow_decision(decision_id: int, db: DbSession) -> dict:
    row = db.get(V4ShadowDecision, decision_id)
    if row is None:
        raise NotFoundError(f"no V4 shadow decision with id {decision_id}")
    observations = db.query(V4ShadowObservation).filter_by(shadow_decision_id=decision_id).all()
    settlement = (
        db.query(V4ShadowSettlement).filter_by(shadow_decision_id=decision_id).one_or_none()
    )
    return {
        **_decision_summary(row),
        "observations": [
            {
                "phase": o.phase,
                "candidate_id": o.candidate_id,
                "observed_at": o.observed_at,
                "status": o.status,
                "net_executable_value": o.net_executable_value,
                "failure_category": o.failure_category,
                "failure_detail": o.failure_detail,
                "market_data_quality": o.market_data_quality,
            }
            for o in observations
        ],
        "settlement": (
            {
                "settled_at": settlement.settled_at,
                "status": settlement.status,
                "entry_net_value": settlement.entry_net_value,
                "exit_net_value": settlement.exit_net_value,
                "realized_pnl": settlement.realized_pnl,
                "return_on_standardized_capital": settlement.return_on_standardized_capital,
                "failure_category": settlement.failure_category,
            }
            if settlement is not None
            else None
        ),
    }


@router.get("/decisions/{decision_id}/candidates")
def get_shadow_candidates(decision_id: int, db: DbSession) -> dict:
    if db.get(V4ShadowDecision, decision_id) is None:
        raise NotFoundError(f"no V4 shadow decision with id {decision_id}")
    rows = (
        db.query(V4ShadowCandidate)
        .filter_by(shadow_decision_id=decision_id)
        .order_by(V4ShadowCandidate.rank.is_(None), V4ShadowCandidate.rank)
        .all()
    )
    legs_by_candidate: dict[int, list[V4ShadowCandidateLeg]] = {}
    for leg in (
        db.query(V4ShadowCandidateLeg)
        .filter(V4ShadowCandidateLeg.shadow_candidate_id.in_([r.id for r in rows] or [0]))
        .order_by(V4ShadowCandidateLeg.leg_index)
        .all()
    ):
        legs_by_candidate.setdefault(leg.shadow_candidate_id, []).append(leg)

    return {
        "notice": EXPERIMENTAL_NOTICE,
        "candidates": [
            {
                "candidate_id": r.candidate_id,
                "rank": r.rank,
                "strategy": r.strategy,
                "expiration": r.expiration,
                "geometry_variant_id": r.geometry_variant_id,
                "validity_status": r.validity_status,
                "status_reason": r.status_reason,
                "semantic": {
                    "compatibility": r.semantic_compatibility,
                    "tier": r.semantic_tier,
                    "reason_codes": r.semantic_reason_codes,
                },
                # CORE and STRESS are surfaced as separate objects so a
                # reader cannot accidentally average them together.
                "core": {
                    "worst_return": r.core_worst_return,
                    "median_return": r.core_median_return,
                    "best_return": r.core_best_return,
                    "positive_scenario_fraction": r.core_positive_scenario_fraction,
                    "positive_region_count": r.core_positive_region_count,
                    "region_count": r.core_region_count,
                    "scenario_average_return": r.core_scenario_average_return,
                    "scenarios_valued": r.core_scenarios_valued,
                    "no_profitable_region": r.no_profitable_region,
                    "profit_concentrated_in_single_region": (
                        r.profit_concentrated_in_single_region
                    ),
                },
                "tail_stress": {
                    "worst_return": r.stress_worst_return,
                    "large_move_survival": r.stress_large_move_survival,
                    "vs_core_worst_delta": r.stress_vs_core_worst_delta,
                    "scenarios_valued": r.stress_scenarios_valued,
                    "note": (
                        "deterministic stress points -- no probability mass, never averaged "
                        "into core statistics"
                    ),
                },
                "execution": {
                    "mean_relative_spread": r.mean_relative_spread,
                    "worst_relative_spread": r.worst_relative_spread,
                    "two_sided_leg_count": r.two_sided_leg_count,
                    "leg_count": r.leg_count,
                    "required_sides_complete": r.required_sides_complete,
                    "max_leg_timestamp_skew_seconds": r.max_leg_timestamp_skew_seconds,
                    "earliest_leg_observed_at": r.earliest_leg_observed_at,
                    "latest_leg_observed_at": r.latest_leg_observed_at,
                    "market_data_quality": r.market_data_quality,
                },
                "capital": {
                    "standardized_capital": r.standardized_capital,
                    "entry_cash_required": r.entry_cash_required,
                    "capital_utilisation": r.capital_utilisation,
                },
                "ranking_key": r.ranking_key,
                # Section 26 -- the frozen per-scenario surface, CORE and
                # TAIL STRESS as separate lists.
                "scenario_grid": r.scenario_grid,
                "rank_explanation": r.rank_explanation,
                "data_quality_warnings": r.data_quality_warnings,
                "legs": [
                    {
                        "leg_index": leg.leg_index,
                        "action": leg.action,
                        "right": leg.right,
                        "strike": leg.strike,
                        "quantity": leg.quantity,
                        "external_contract_id": leg.external_contract_id,
                        "required_side": leg.required_side,
                        "required_side_price": leg.required_side_price,
                        "bid": leg.bid,
                        "ask": leg.ask,
                        "implied_volatility": leg.implied_volatility,
                        "market_data_quality": leg.market_data_quality,
                        "source_provider": leg.source_provider,
                        "retrieved_at": leg.retrieved_at,
                    }
                    for leg in legs_by_candidate.get(r.id, [])
                ],
            }
            for r in rows
        ],
    }


@router.get("/track-record")
def get_shadow_track_record(db: DbSession) -> dict:
    """Sections 61/67 -- counts and raw outcomes only. No win rate, no
    expectancy, no performance claim at a sample this small, and the
    cohort is never merged with the V3 benchmark."""
    total = db.query(V4ShadowDecision).count()
    ranked = db.query(V4ShadowDecision).filter_by(status="RANKED").count()
    no_action = db.query(V4ShadowDecision).filter_by(status="NO_ACTION").count()
    failed = db.query(V4ShadowDecision).filter_by(status="FAILED").count()
    settled = db.query(V4ShadowSettlement).filter_by(status="SETTLED").count()
    settlement_failed = db.query(V4ShadowSettlement).filter_by(status="OBSERVATION_FAILED").count()
    entry_observed = (
        db.query(V4ShadowObservation).filter_by(phase="ENTRY", status="OBSERVED").count()
    )
    entry_not_executable = (
        db.query(V4ShadowObservation).filter_by(phase="ENTRY", status="NOT_EXECUTABLE").count()
    )

    #: Below this, no aggregate is reported at all -- a median of three
    #: is not a result, and printing one invites exactly the reading this
    #: phase exists to prevent.
    minimum_meaningful_sample = 30
    return {
        "notice": EXPERIMENTAL_NOTICE,
        "cohort": "V4 Experimental Shadow",
        "counts": {
            "shadow_decisions": total,
            "ranked": ranked,
            "no_action": no_action,
            "failed": failed,
            "entry_observed": entry_observed,
            "entry_not_executable": entry_not_executable,
            "settled": settled,
            "settlement_failed": settlement_failed,
        },
        "sample_sufficiency": (
            "INSUFFICIENT SAMPLE"
            if settled < minimum_meaningful_sample
            else "sample present -- still requires forward validation"
        ),
        "minimum_meaningful_sample": minimum_meaningful_sample,
        "performance_note": (
            "Counts and raw outcomes only. No win rate, expectancy, or comparison against the "
            "V3 official cohort is computed at this sample size."
        ),
    }


# ---------------------------------------------------------------------------
# Six-configuration read model (V4 consolidation, Sections 53-54).
#
# ONE response carrying the event-level common evidence, all six
# configuration results, and a compact summary of every shared candidate --
# three queries total, never a fetch chain per configuration. The frontend
# switches between the six results client-side without another request.
# ---------------------------------------------------------------------------
_CONFIG_DISPLAY_ORDER = [
    "v4_2k_conservative",
    "v4_2k_moderate",
    "v4_2k_aggressive",
    "v4_10k_conservative",
    "v4_10k_moderate",
    "v4_10k_aggressive",
]


def _config_result_summary(row: V4ShadowConfigResult) -> dict:
    return {
        "configuration_key": row.configuration_key,
        "label": f"${int(row.capital_base):,} {row.risk_profile.title()}",
        "capital_base": row.capital_base,
        "risk_profile": row.risk_profile,
        "configuration_version": row.configuration_version,
        "max_risk_dollars": row.max_risk_dollars,
        "max_risk_utilization_pct": row.max_risk_utilization_pct,
        "status": row.status,
        "no_action_reason": row.no_action_reason,
        "rank_1_candidate_id": row.rank_1_candidate_id,
        "eligible_candidate_count": row.eligible_candidate_count,
        "excluded_candidate_count": row.excluded_candidate_count,
        "exclusions": row.exclusions or [],
        "ranked_candidate_ids": row.ranked_candidate_ids or [],
        "ranking_version": row.ranking_version,
    }


@router.get("/decisions/{decision_id}/configurations")
def get_shadow_decision_configurations(decision_id: int, db: DbSession) -> dict:
    """Event-level evidence + six configuration results + shared candidate
    summaries, in one round trip."""
    decision = db.get(V4ShadowDecision, decision_id)
    if decision is None:
        raise NotFoundError(f"no V4 shadow decision with id {decision_id}")

    config_rows = {
        r.configuration_key: r
        for r in db.query(V4ShadowConfigResult).filter_by(shadow_decision_id=decision_id)
    }
    candidates = (
        db.query(V4ShadowCandidate)
        .filter_by(shadow_decision_id=decision_id)
        .order_by(V4ShadowCandidate.rank.is_(None), V4ShadowCandidate.rank)
        .all()
    )
    by_id = {c.candidate_id: c for c in candidates}

    def candidate_summary(c: V4ShadowCandidate) -> dict:
        return {
            "candidate_id": c.candidate_id,
            "unconstrained_rank": c.rank,
            "strategy": c.strategy,
            "expiration": c.expiration,
            "validity_status": c.validity_status,
            "semantic_tier": c.semantic_tier,
            "core_worst_return": c.core_worst_return,
            "core_median_return": c.core_median_return,
            "core_positive_scenario_fraction": c.core_positive_scenario_fraction,
            "stress_worst_return": c.stress_worst_return,
            "mean_relative_spread": c.mean_relative_spread,
            "entry_cash_required": c.entry_cash_required,
            "market_data_quality": c.market_data_quality,
            "rank_explanation": c.rank_explanation,
        }

    # Lifecycle (activation phase, Section 20): derived from each
    # configuration's OWN entry and settlement rows. No configuration ever
    # borrows the event-level observation.
    entry_obs = (
        db.query(V4ShadowObservation)
        .filter_by(shadow_decision_id=decision_id, phase="ENTRY")
        .first()
    )
    settlement = (
        db.query(V4ShadowSettlement)
        .filter_by(shadow_decision_id=decision_id)
        .order_by(V4ShadowSettlement.id.desc())
        .first()
    )
    entries_by_result = {
        e.shadow_config_result_id: e
        for e in db.query(V4ShadowConfigEntry).filter_by(shadow_decision_id=decision_id)
    }
    settlements_by_result = {
        x.shadow_config_result_id: x
        for x in db.query(V4ShadowConfigSettlement).filter_by(shadow_decision_id=decision_id)
    }
    cand_obs = {
        (o.candidate_id, o.phase): o
        for o in db.query(V4ShadowCandidateObservation).filter_by(shadow_decision_id=decision_id)
    }

    def lifecycle(row: V4ShadowConfigResult) -> str:
        if row.status == "NO_ACTION":
            return "NO_ACTION"
        if row.status == "FAILED":
            return "FAILED"
        entry = entries_by_result.get(row.id)
        if entry is None:
            return "WAITING_ENTRY"
        if entry.status != "OBSERVED":
            return "ENTRY_FAILED"
        stl = settlements_by_result.get(row.id)
        if stl is None:
            return "WAITING_SETTLEMENT"
        return "SETTLED" if stl.status == "SETTLED" else "SETTLEMENT_FAILED"

    def entry_payload(row: V4ShadowConfigResult) -> dict | None:
        e = entries_by_result.get(row.id)
        if e is None:
            return None
        obs = cand_obs.get((e.candidate_id, "ENTRY"))
        return {
            "status": e.status,
            "candidate_id": e.candidate_id,
            "quantity": e.quantity,
            "standardized_capital": e.standardized_capital,
            "capital_used": e.capital_used,
            "max_risk_per_contract": e.max_risk_per_contract,
            "max_risk_used": e.max_risk_used,
            "entry_net_value": e.entry_net_value,
            "pricing_convention": e.pricing_convention,
            "observed_at": e.observed_at,
            "market_data_quality": e.market_data_quality,
            "failure_category": e.failure_category,
            "failure_detail": e.failure_detail,
            "timing_policy_version": e.timing_policy_version,
            "legs": (obs.legs_json or {}).get("legs") if obs else None,
            "earliest_leg_observed_at": obs.earliest_leg_observed_at if obs else None,
            "latest_leg_observed_at": obs.latest_leg_observed_at if obs else None,
            "max_leg_timestamp_skew_seconds": obs.max_leg_timestamp_skew_seconds if obs else None,
        }

    def settlement_payload(row: V4ShadowConfigResult) -> dict | None:
        x = settlements_by_result.get(row.id)
        if x is None:
            return None
        obs = cand_obs.get((x.candidate_id, "EXIT"))
        return {
            "status": x.status,
            "candidate_id": x.candidate_id,
            "quantity": x.quantity,
            "standardized_capital": x.standardized_capital,
            "capital_used": x.capital_used,
            "entry_net_value": x.entry_net_value,
            "exit_net_value": x.exit_net_value,
            "realized_pnl": x.realized_pnl,
            "return_on_standardized_capital": x.return_on_standardized_capital,
            "entry_observed_at": x.entry_observed_at,
            "settled_at": x.settled_at,
            "pricing_convention": x.pricing_convention,
            "market_data_quality": x.market_data_quality,
            "failure_category": x.failure_category,
            "failure_detail": x.failure_detail,
            "legs": (obs.legs_json or {}).get("legs") if obs else None,
        }

    configurations = []
    for key in _CONFIG_DISPLAY_ORDER:
        row = config_rows.get(key)
        if row is None:
            continue
        summary = _config_result_summary(row)
        top = by_id.get(row.rank_1_candidate_id) if row.rank_1_candidate_id else None
        summary["rank_1"] = candidate_summary(top) if top is not None else None
        summary["lifecycle"] = lifecycle(row)
        summary["entry"] = entry_payload(row)
        summary["settlement"] = settlement_payload(row)
        configurations.append(summary)

    return {
        "notice": EXPERIMENTAL_NOTICE,
        "decision": _decision_summary(decision),
        "timing_policy_version": decision.decision_timing_policy_version,
        "configurations": configurations,
        "candidates": [candidate_summary(c) for c in candidates],
        "default_configuration_key": "v4_2k_moderate",
        "entry_observation": None
        if entry_obs is None
        else {
            "status": entry_obs.status,
            "candidate_id": entry_obs.candidate_id,
            "observed_at": entry_obs.observed_at,
            "failure_category": entry_obs.failure_category,
            "failure_detail": entry_obs.failure_detail,
            "market_data_quality": entry_obs.market_data_quality,
            "net_executable_value": entry_obs.net_executable_value,
        },
        "settlement": None
        if settlement is None
        else {
            "status": settlement.status,
            "settled_at": settlement.settled_at,
            "failure_category": settlement.failure_category,
            "failure_detail": settlement.failure_detail,
            "entry_net_value": settlement.entry_net_value,
            "exit_net_value": settlement.exit_net_value,
            "realized_pnl": settlement.realized_pnl,
            "return_on_standardized_capital": settlement.return_on_standardized_capital,
            "market_data_quality": settlement.market_data_quality,
        },
        "settlement_policy": "T+1 at 15:55 ET on the first post-earnings trading day",
    }


# ---------------------------------------------------------------------------
# Per-configuration track record (V4 consolidation, Sections 28-31).
# Counts only. No win rate, expectancy or portfolio statistic is computed
# below the sample floor -- and no portfolio drawdown or Sharpe is computed
# at all, because there is no real capital ledger yet (Section 31).
# ---------------------------------------------------------------------------
SAMPLE_FLOOR = 30


@router.get("/track-record/by-configuration")
def get_shadow_track_record_by_configuration(db: DbSession) -> dict:
    from sqlalchemy import func

    def counts(model, status_col, key_col):
        out: dict[str, dict[str, int]] = {}
        for key, status, n in (
            db.query(key_col, status_col, func.count(model.id)).group_by(key_col, status_col).all()
        ):
            out.setdefault(key, {})[status] = n
        return out

    results = counts(
        V4ShadowConfigResult, V4ShadowConfigResult.status, V4ShadowConfigResult.configuration_key
    )
    entries = counts(
        V4ShadowConfigEntry, V4ShadowConfigEntry.status, V4ShadowConfigEntry.configuration_key
    )
    settlements = counts(
        V4ShadowConfigSettlement,
        V4ShadowConfigSettlement.status,
        V4ShadowConfigSettlement.configuration_key,
    )
    # Per-cohort realized outcomes (only this cohort's own settlements).
    realized: dict[str, list] = {}
    for x in db.query(V4ShadowConfigSettlement).filter_by(status="SETTLED"):
        realized.setdefault(x.configuration_key, []).append(x)

    rows = []
    for key in _CONFIG_DISPLAY_ORDER:
        r = results.get(key, {})
        e = entries.get(key, {})
        st = settlements.get(key, {})
        settled_rows = realized.get(key, [])
        n_settled = len(settled_rows)
        sufficient = n_settled >= SAMPLE_FLOOR
        wins = sum(1 for x in settled_rows if (x.realized_pnl or 0) > 0)
        losses = sum(1 for x in settled_rows if (x.realized_pnl or 0) < 0)
        rets = sorted(
            float(x.return_on_standardized_capital)
            for x in settled_rows
            if x.return_on_standardized_capital is not None
        )
        rows.append(
            {
                "configuration_key": key,
                "events": sum(r.values()),
                "actionable": r.get("RANKED", 0),
                "no_action": r.get("NO_ACTION", 0),
                "failed": r.get("FAILED", 0),
                "entry_observed": e.get("OBSERVED", 0),
                "entry_failed": e.get("NOT_EXECUTABLE", 0),
                "settled": st.get("SETTLED", 0),
                "settlement_failed": st.get("OBSERVATION_FAILED", 0),
                "wins": wins if sufficient else None,
                "losses": losses if sufficient else None,
                "win_rate": (wins / n_settled) if sufficient and n_settled else None,
                "average_standardized_return": (sum(rets) / len(rets))
                if sufficient and rets
                else None,
                "median_standardized_return": (rets[len(rets) // 2])
                if sufficient and rets
                else None,
                "average_realized_pnl": (
                    float(sum(x.realized_pnl or 0 for x in settled_rows)) / n_settled
                    if sufficient and n_settled
                    else None
                ),
                "average_capital_used": (
                    float(sum(x.capital_used or 0 for x in settled_rows)) / n_settled
                    if sufficient and n_settled
                    else None
                ),
                "sample_sufficiency": "SUFFICIENT" if sufficient else "INSUFFICIENT SAMPLE",
            }
        )
    return {
        "notice": EXPERIMENTAL_NOTICE,
        "sample_floor": SAMPLE_FLOOR,
        "metrics_note": (
            "Each cohort's counts and outcomes come only from that cohort's own entry and "
            "settlement observations. Win rate, average/median standardized return and realized "
            f"P&L are reported only once a cohort has at least {SAMPLE_FLOOR} settled "
            "observations. "
            "No portfolio drawdown or Sharpe is computed: there is no real capital ledger yet, and "
            "V3's static-$2,000 accounting is not reproduced."
        ),
        "configurations": rows,
    }


# ---------------------------------------------------------------------------
# Same-event comparison (V4 consolidation, Sections 33-35).
# V3 control and the six V4 configurations for ONE earnings event, side by
# side, with their DIFFERENT timing policies stated explicitly. V3 numbers
# and V4 numbers are returned in separate objects and are never combined.
# ---------------------------------------------------------------------------
@router.get("/events/{event_id}/comparison")
def get_same_event_comparison(event_id: int, db: DbSession) -> dict:
    from analytics.decision_timing_policy import V3_TIMING_POLICY, V4_TIMING_POLICY
    from models.decision_snapshot import DecisionSnapshot
    from models.earnings_calendar_event import EarningsCalendarEvent
    from models.entry_capture_attempt import EntryCaptureAttempt
    from models.settlement_snapshot import SettlementSnapshot

    event = db.get(EarningsCalendarEvent, event_id)
    if event is None:
        raise NotFoundError(f"no earnings calendar event with id {event_id}")

    # --- V3 control -------------------------------------------------------
    v3_decision = (
        db.query(DecisionSnapshot)
        .filter_by(earnings_calendar_event_id=event_id)
        .order_by(DecisionSnapshot.id.desc())
        .first()
    )
    v3: dict | None = None
    if v3_decision is not None:
        entry = (
            db.query(EntryCaptureAttempt)
            .filter_by(decision_snapshot_id=v3_decision.id)
            .order_by(EntryCaptureAttempt.id.desc())
            .first()
        )
        settlement = (
            db.query(SettlementSnapshot)
            .filter_by(decision_id=v3_decision.id)
            .order_by(SettlementSnapshot.id.desc())
            .first()
        )
        v3 = {
            "engine": "V3 historical control",
            "timing_policy_version": V3_TIMING_POLICY.version,
            "observation_time_et": V3_TIMING_POLICY.entry_time.strftime("%H:%M"),
            "decision_id": v3_decision.id,
            "generated_at": v3_decision.generated_at,
            "strategy": v3_decision.strategy_type,
            "direction": v3_decision.strategy_direction,
            "risk_profile": v3_decision.effective_risk_profile,
            "underlying_price": v3_decision.underlying_price,
            "entry": None
            if entry is None
            else {
                "status": entry.status,
                "capture_error": entry.capture_error,
                "contracts": entry.contracts,
                "net_entry_cash": entry.net_entry_cash,
                "initial_max_risk": entry.initial_max_risk,
                "source_provider": entry.source_provider,
            },
            "settlement": None
            if settlement is None
            else {
                "status": settlement.status,
                "realized_pnl": settlement.realized_pnl,
            },
        }

    # --- V4 six configurations -------------------------------------------
    v4_decision = (
        db.query(V4ShadowDecision)
        .filter_by(earnings_calendar_event_id=event_id)
        .order_by(V4ShadowDecision.id.desc())
        .first()
    )
    v4: dict | None = None
    if v4_decision is not None:
        config_rows = {
            r.configuration_key: r
            for r in db.query(V4ShadowConfigResult).filter_by(shadow_decision_id=v4_decision.id)
        }
        candidates = {
            c.candidate_id: c
            for c in db.query(V4ShadowCandidate).filter_by(shadow_decision_id=v4_decision.id)
        }
        v4_settlement = (
            db.query(V4ShadowSettlement)
            .filter_by(shadow_decision_id=v4_decision.id)
            .order_by(V4ShadowSettlement.id.desc())
            .first()
        )
        entry_obs = (
            db.query(V4ShadowObservation)
            .filter_by(shadow_decision_id=v4_decision.id, phase="ENTRY")
            .first()
        )
        cfg_entries = {
            e.shadow_config_result_id: e
            for e in db.query(V4ShadowConfigEntry).filter_by(shadow_decision_id=v4_decision.id)
        }
        cfg_settlements = {
            x.shadow_config_result_id: x
            for x in db.query(V4ShadowConfigSettlement).filter_by(shadow_decision_id=v4_decision.id)
        }
        configs = []
        for key in _CONFIG_DISPLAY_ORDER:
            row = config_rows.get(key)
            if row is None:
                continue
            top = candidates.get(row.rank_1_candidate_id) if row.rank_1_candidate_id else None
            ce = cfg_entries.get(row.id)
            cs = cfg_settlements.get(row.id)
            configs.append(
                {
                    "entry": None
                    if ce is None
                    else {
                        "status": ce.status,
                        "quantity": ce.quantity,
                        "capital_used": ce.capital_used,
                        "entry_net_value": ce.entry_net_value,
                        "observed_at": ce.observed_at,
                        "market_data_quality": ce.market_data_quality,
                    },
                    "settlement": None
                    if cs is None
                    else {
                        "status": cs.status,
                        "realized_pnl": cs.realized_pnl,
                        "return_on_standardized_capital": cs.return_on_standardized_capital,
                        "settled_at": cs.settled_at,
                    },
                    "configuration_key": key,
                    "label": f"${int(row.capital_base):,} {row.risk_profile.title()}",
                    "status": row.status,
                    "no_action_reason": row.no_action_reason,
                    "capital_base": row.capital_base,
                    "max_risk_dollars": row.max_risk_dollars,
                    "strategy": top.strategy if top else None,
                    "expiration": top.expiration if top else None,
                    "entry_cash_required": top.entry_cash_required if top else None,
                    "core_median_return": top.core_median_return if top else None,
                    "core_worst_return": top.core_worst_return if top else None,
                    "stress_worst_return": top.stress_worst_return if top else None,
                }
            )
        v4 = {
            "engine": "V4 experimental shadow",
            "timing_policy_version": v4_decision.decision_timing_policy_version
            or V4_TIMING_POLICY.version,
            "observation_time_et": V4_TIMING_POLICY.entry_time.strftime("%H:%M"),
            "decision_id": v4_decision.id,
            "generated_at": v4_decision.generated_at,
            "underlying_price": v4_decision.underlying_price,
            "market_data_quality": v4_decision.market_data_quality,
            "entry_observation": None
            if entry_obs is None
            else {
                "status": entry_obs.status,
                "candidate_id": entry_obs.candidate_id,
            },
            "settlement": None
            if v4_settlement is None
            else {
                "status": v4_settlement.status,
                "realized_pnl": v4_settlement.realized_pnl,
                "return_on_standardized_capital": v4_settlement.return_on_standardized_capital,
            },
            "configurations": configs,
        }

    return {
        "notice": EXPERIMENTAL_NOTICE,
        "event": {
            "id": event.id,
            "symbol": event.symbol,
            "company_name": event.company_name,
            "earnings_date": event.earnings_date,
            "earnings_time": event.earnings_time,
        },
        "timing_note": (
            "V3 observes at 15:55 ET and V4 at 15:30 ET. Their entry prices are taken from "
            "different moments of the session; settlement for both is 15:55 ET on the first "
            "post-earnings trading day. This is not a timestamp-identical comparison."
        ),
        "v3_control": v3,
        "v4_shadow": v4,
    }
