"""Six-cohort forward evidence (V4 activation phase, Sections 4-16).

ENTRY (no I/O). At freeze time every configuration has its own rank #1 (or
NO_ACTION). The unique selected candidates are observed ONCE each from the
quotes already frozen on their legs -- exactly the prices the ranking used
-- and every configuration that selected a candidate freezes its own
position (quantity, capital used, max risk, entry value) against that one
shared observation. Buy legs at ASK, sell legs at BID; a missing required
side makes the candidate observation NOT_EXECUTABLE and every configuration
holding it ENTRY_FAILED. No midpoint, no last, no historical fallback.

SETTLEMENT (one quote sweep). At the legal exit window the unique contracts
across all held candidates are re-quoted BY conId, once per expiration
group -- never once per configuration -- then each configuration's realized
result is computed for its own frozen quantity. Close longs at BID, close
shorts at ASK. A candidate whose exit side is missing fails every
configuration that holds it, and only those.

Both directions are idempotent by construction (one row per configuration
result) and isolate failures per candidate/configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from analytics.decision.v4_4b_ranking import RankableCandidate
from analytics.decision.v4_configurations import (
    V4_CONFIGURATION_VERSION,
    get_configuration,
    size_configuration_position,
)
from analytics.decision_timing_policy import V4_ACTIVE_TIMING_POLICY
from models.v4_shadow import (
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowCandidateObservation,
    V4ShadowConfigEntry,
    V4ShadowConfigResult,
    V4ShadowConfigSettlement,
    V4ShadowDecision,
    V4ShadowRunEvent,
    V4ShadowSettlement,
)
from providers.base import OptionsDataProvider
from providers.types import KnownContract
from services.v4_config_evaluation import max_defined_risk

log = logging.getLogger("services.v4_shadow_cohort")

ENTRY_CONVENTION = "BUY_AT_ASK_SELL_AT_BID"
EXIT_CONVENTION = "CLOSE_LONG_AT_BID_CLOSE_SHORT_AT_ASK"


@dataclass
class CohortEntrySummary:
    unique_candidates: list[str] = field(default_factory=list)
    unique_contracts: int = 0
    entries_observed: int = 0
    entries_failed: int = 0
    no_action: int = 0
    by_configuration: dict[str, str] = field(default_factory=dict)


# V4 required-side settlement incident (2026-09-04). "required exit side
# missing" was one bucket for two genuinely different worlds: a quote that
# never arrived, and a quote that arrived saying the book is empty. Only the
# first is worth retrying, and only the second is a real market fact. These
# helpers keep them apart in the persisted evidence and in Operations.
EXIT_NO_BID = "NO_BID"
EXIT_NO_ASK = "NO_ASK"
EXIT_NO_EXECUTABLE_SIDE = "NO_EXECUTABLE_SIDE"
EXIT_REQUIRED_SIDE_TIMEOUT = "REQUIRED_SIDE_TIMEOUT"
EXIT_REQUIRED_SIDE_MISSING = "REQUIRED_SIDE_QUOTE_MISSING"


def _required_side_state(quote, side: str) -> str:
    """present | book_empty | unavailable -- what the provider actually said
    about the ONE side this leg must be closed on."""
    if quote is None:
        return "unavailable"
    if (getattr(quote, side, None)) is not None:
        return "present"
    empty = getattr(quote, f"{side}_book_empty", None)
    return "book_empty" if empty else "unavailable"


def _exit_failure_category(missing_rows: list[dict]) -> str:
    states = {r["required_side_state"] for r in missing_rows}
    sides = {r["required_side"] for r in missing_rows}
    if states == {"book_empty"}:
        if sides == {"bid"}:
            return EXIT_NO_BID
        if sides == {"ask"}:
            return EXIT_NO_ASK
        return EXIT_NO_EXECUTABLE_SIDE
    if states == {"unavailable"}:
        return EXIT_REQUIRED_SIDE_TIMEOUT
    return EXIT_REQUIRED_SIDE_MISSING


def _exit_failure_detail(missing_rows: list[dict]) -> str:
    """Section 25 -- say what was actually observed, per leg, instead of only
    naming the leg indices."""
    parts = []
    for r in missing_rows:
        parts.append(
            f"leg {r['leg_index']} ({r['action']} {r['right']} {r['strike']}, "
            f"conId {r['external_contract_id']}) needs {r['required_side'].upper()}: "
            f"{r['required_side_state']}; bid={r['bid']} ask={r['ask']} last={r['last']} "
            f"bid_size={r['bid_size']} ask_size={r['ask_size']} "
            f"quality={r['market_data_quality']}"
        )
    return (
        "required exit side missing on leg(s) "
        f"{[r['leg_index'] for r in missing_rows]} -- no midpoint, last-price, "
        "historical or intrinsic substitution is permitted. " + " | ".join(parts)
    )


def _executable_entry_legs(legs) -> tuple[Decimal | None, list[int], list[dict], list[datetime]]:
    """ENTRY sides from the frozen leg inputs (V4T1LegInput) for ONE unit."""
    net = Decimal(0)
    missing: list[int] = []
    rows: list[dict] = []
    stamps: list[datetime] = []
    for leg in legs:
        side = "ask" if leg.action == "buy" else "bid"
        price = leg.entry_ask if leg.action == "buy" else leg.entry_bid
        sign = Decimal(1) if leg.action == "buy" else Decimal(-1)
        if price is None:
            missing.append(leg.leg_index)
        else:
            net += sign * price * Decimal(leg.quantity) * leg.multiplier
        rows.append(
            {
                "leg_index": leg.leg_index,
                "action": leg.action,
                "right": leg.right,
                "strike": str(leg.strike),
                "external_contract_id": leg.external_contract_id,
                "required_side": side,
                "price": None if price is None else str(price),
                "bid": None if leg.entry_bid is None else str(leg.entry_bid),
                "ask": None if leg.entry_ask is None else str(leg.entry_ask),
                "implied_volatility": None if leg.entry_iv is None else str(leg.entry_iv),
                "market_data_quality": leg.market_data_quality,
            }
        )
    return (None if missing else net), missing, rows, stamps


def freeze_config_entries(
    db: Session,
    *,
    decision: V4ShadowDecision,
    config_rows: list[V4ShadowConfigResult],
    rankable_by_id: dict[str, RankableCandidate],
    leg_retrieved_at_by_id: dict[str, dict[int, datetime]],
    observed_at: datetime,
    market_data_quality: str | None,
) -> CohortEntrySummary:
    """Section 5-10. Pure over frozen data: no provider call is made here."""
    summary = CohortEntrySummary()
    observations: dict[str, V4ShadowCandidateObservation] = {}

    # 1. One candidate-level ENTRY observation per UNIQUE selected candidate.
    selected = sorted(
        {
            r.rank_1_candidate_id
            for r in config_rows
            if r.status == "RANKED" and r.rank_1_candidate_id
        }
    )
    all_contracts: set[str] = set()
    for candidate_id in selected:
        rankable = rankable_by_id.get(candidate_id)
        if rankable is None:
            continue
        legs = rankable.context.legs
        net, missing, rows, _ = _executable_entry_legs(legs)
        stamps = [
            t for t in (leg_retrieved_at_by_id.get(candidate_id) or {}).values() if t is not None
        ]
        contracts = {str(leg.external_contract_id) for leg in legs if leg.external_contract_id}
        all_contracts |= contracts
        obs = V4ShadowCandidateObservation(
            shadow_decision_id=decision.id,
            candidate_id=candidate_id,
            phase="ENTRY",
            observed_at=observed_at,
            status="NOT_EXECUTABLE" if missing else "OBSERVED",
            failure_category="REQUIRED_SIDE_QUOTE_MISSING" if missing else None,
            failure_detail=(
                f"required entry side missing on leg(s) {missing} -- no midpoint, last-price "
                "or historical substitution is permitted"
                if missing
                else None
            ),
            net_executable_value=net,
            market_data_quality=market_data_quality,
            source_provider="ibkr_tws",
            earliest_leg_observed_at=min(stamps) if stamps else None,
            latest_leg_observed_at=max(stamps) if stamps else None,
            max_leg_timestamp_skew_seconds=(
                Decimal(str((max(stamps) - min(stamps)).total_seconds()))
                if len(stamps) > 1
                else (Decimal(0) if stamps else None)
            ),
            unique_contract_count=len(contracts) or None,
            legs_json={"legs": rows, "pricing_convention": ENTRY_CONVENTION},
        )
        db.add(obs)
        observations[candidate_id] = obs
        summary.unique_candidates.append(candidate_id)
    db.flush()
    summary.unique_contracts = len(all_contracts)

    # 2. One CONFIGURATION entry per RANKED configuration, sized independently.
    for row in config_rows:
        if row.status != "RANKED" or not row.rank_1_candidate_id:
            summary.no_action += 1 if row.status == "NO_ACTION" else 0
            summary.by_configuration[row.configuration_key] = row.status
            continue
        cand_obs: V4ShadowCandidateObservation | None = observations.get(row.rank_1_candidate_id)
        rankable = rankable_by_id.get(row.rank_1_candidate_id)
        if cand_obs is None or rankable is None:  # pragma: no cover -- defensive
            continue
        try:
            configuration = get_configuration(row.configuration_key)
            per_cash = rankable.entry_cash_required or Decimal(0)
            per_risk = max_defined_risk(rankable) or per_cash
            position = size_configuration_position(
                configuration,
                candidate_id=row.rank_1_candidate_id,
                per_contract_entry_cash=per_cash,
                per_contract_max_risk=per_risk,
            )
            observed = cand_obs.status == "OBSERVED"
            entry = V4ShadowConfigEntry(
                shadow_config_result_id=row.id,
                shadow_decision_id=decision.id,
                candidate_observation_id=cand_obs.id,
                configuration_key=row.configuration_key,
                candidate_id=row.rank_1_candidate_id,
                status="OBSERVED" if observed else "NOT_EXECUTABLE",
                quantity=position.quantity,
                standardized_capital=position.standardized_capital,
                capital_used=position.capital_used,
                max_risk_per_contract=position.per_contract_max_risk,
                max_risk_used=position.max_risk_used,
                entry_net_value=(
                    (cand_obs.net_executable_value or Decimal(0)) * position.quantity
                    if observed
                    else None
                ),
                pricing_convention=ENTRY_CONVENTION,
                observed_at=observed_at,
                market_data_quality=market_data_quality,
                failure_category=None if observed else cand_obs.failure_category,
                failure_detail=None if observed else cand_obs.failure_detail,
                timing_policy_version=V4_ACTIVE_TIMING_POLICY.version,
                engine_version=decision.engine_version,
                configuration_version=V4_CONFIGURATION_VERSION,
            )
            db.add(entry)
            summary.by_configuration[row.configuration_key] = entry.status
            if observed:
                summary.entries_observed += 1
            else:
                summary.entries_failed += 1
        except Exception as exc:  # noqa: BLE001 -- Section 10: one config never fails five
            log.error("config entry freeze failed for %s", row.configuration_key, exc_info=True)
            summary.by_configuration[row.configuration_key] = f"FAILED:{type(exc).__name__}"
            summary.entries_failed += 1
    db.flush()
    return summary


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
@dataclass
class CohortSettlementSummary:
    unique_candidates: int = 0
    unique_contracts: int = 0
    quote_calls: int = 0
    settled: int = 0
    failed: int = 0
    skipped_already: int = 0
    by_configuration: dict[str, str] = field(default_factory=dict)


def settle_shadow_decision_cohorts(
    db: Session,
    *,
    provider: OptionsDataProvider,
    decision: V4ShadowDecision,
    observed_at: datetime,
    timing_policy_version: str = V4_ACTIVE_TIMING_POLICY.version,
) -> CohortSettlementSummary:
    """Sections 11-16. Settles the exact frozen positions. Never raises.

    ``timing_policy_version`` is the policy the EXIT is observed under and is
    written on every settlement row; the decision/entry rows keep their own
    frozen version (prospective transition, no rewritten history)."""
    summary = CohortSettlementSummary()
    entries = (
        db.query(V4ShadowConfigEntry)
        .filter_by(shadow_decision_id=decision.id, status="OBSERVED")
        .all()
    )
    already = {
        s.shadow_config_result_id
        for s in db.query(V4ShadowConfigSettlement).filter_by(shadow_decision_id=decision.id)
    }
    pending = [e for e in entries if e.shadow_config_result_id not in already]
    summary.skipped_already = len(entries) - len(pending)
    if not pending:
        return summary

    # Frozen candidates + legs, by candidate id.
    candidates = {
        c.candidate_id: c
        for c in db.query(V4ShadowCandidate).filter_by(shadow_decision_id=decision.id)
        if c.candidate_id in {e.candidate_id for e in pending}
    }
    legs_by_candidate: dict[str, list[V4ShadowCandidateLeg]] = {}
    for leg in (
        db.query(V4ShadowCandidateLeg)
        .filter(
            V4ShadowCandidateLeg.shadow_candidate_id.in_([c.id for c in candidates.values()] or [0])
        )
        .order_by(V4ShadowCandidateLeg.leg_index)
    ):
        cid = next(k for k, v in candidates.items() if v.id == leg.shadow_candidate_id)
        legs_by_candidate.setdefault(cid, []).append(leg)
    summary.unique_candidates = len(candidates)

    # 1. Dedupe unique contracts across ALL held candidates; ONE quote call per
    #    expiration group (contracts are identified by conId, never re-selected).
    by_expiration: dict = {}
    for cid, legs in legs_by_candidate.items():
        for leg in legs:
            if not leg.external_contract_id:
                continue
            by_expiration.setdefault(candidates[cid].expiration, {})[
                str(leg.external_contract_id)
            ] = leg
    quotes_by_conid: dict[str, Any] = {}
    quote_errors: dict = {}
    for expiration, legs in by_expiration.items():
        contracts = [
            KnownContract(
                strike=leg.strike,
                option_type=leg.right,
                external_contract_id=str(leg.external_contract_id),
                action=leg.action,
            )
            for leg in legs.values()
        ]
        summary.quote_calls += 1
        try:
            quotes = provider.get_quotes_for_known_contracts(
                decision.ticker, contracts, expiration, observed_at
            )
        except Exception as exc:  # noqa: BLE001 -- provider boundary, isolated per group
            quote_errors[expiration] = f"{type(exc).__name__}: {exc}"
            continue
        by_key = {(q.strike, q.option_type): q for q in quotes}
        for conid, leg in legs.items():
            q = by_key.get((leg.strike, leg.right))
            if q is not None:
                quotes_by_conid[conid] = q
    summary.unique_contracts = sum(len(v) for v in by_expiration.values())

    # 2. One candidate-level EXIT observation per unique held candidate.
    exit_obs: dict[str, V4ShadowCandidateObservation] = {}
    existing_obs: V4ShadowCandidateObservation | None
    for cid, legs in legs_by_candidate.items():
        existing_obs = (
            db.query(V4ShadowCandidateObservation)
            .filter_by(shadow_decision_id=decision.id, candidate_id=cid, phase="EXIT")
            .one_or_none()
        )
        if existing_obs is not None:
            exit_obs[cid] = existing_obs
            continue
        net = Decimal(0)
        missing: list[int] = []
        rows: list[dict] = []
        qualities: set[str] = set()
        stamps: list[datetime] = []
        for leg in legs:
            q = (
                quotes_by_conid.get(str(leg.external_contract_id))
                if leg.external_contract_id
                else None
            )
            side = "bid" if leg.action == "buy" else "ask"
            price = (q.bid if leg.action == "buy" else q.ask) if q is not None else None
            leg_last = getattr(q, "last_price", None)
            if price is None:
                missing.append(leg.leg_index)
            else:
                sign = Decimal(1) if leg.action == "buy" else Decimal(-1)
                net += sign * price * Decimal(leg.quantity) * (leg.multiplier or Decimal("100"))
            if q is not None:
                if q.market_data_quality:
                    qualities.add(q.market_data_quality)
                if getattr(q, "retrieved_at", None):
                    stamps.append(q.retrieved_at)
            rows.append(
                {
                    "leg_index": leg.leg_index,
                    "action": leg.action,
                    "right": leg.right,
                    "strike": str(leg.strike),
                    "external_contract_id": leg.external_contract_id,
                    "required_side": side,
                    "required_side_state": _required_side_state(q, side),
                    "price": None if price is None else str(price),
                    "bid": None if q is None or q.bid is None else str(q.bid),
                    "ask": None if q is None or q.ask is None else str(q.ask),
                    "last": None if leg_last is None else str(leg_last),
                    "bid_size": getattr(q, "bid_size", None),
                    "ask_size": getattr(q, "ask_size", None),
                    "bid_book_empty": getattr(q, "bid_book_empty", None),
                    "ask_book_empty": getattr(q, "ask_book_empty", None),
                    "market_data_quality": q.market_data_quality if q else None,
                    "retrieved_at": q.retrieved_at.isoformat()
                    if q and getattr(q, "retrieved_at", None)
                    else None,
                }
            )
        expiration = candidates[cid].expiration
        missing_rows = [r for r in rows if r["leg_index"] in missing]
        category = _exit_failure_category(missing_rows) if missing else None
        detail = _exit_failure_detail(missing_rows) if missing else None
        if expiration in quote_errors and missing:
            detail = f"exit quote acquisition failed: {quote_errors[expiration]}"
            category = EXIT_REQUIRED_SIDE_MISSING
        obs = V4ShadowCandidateObservation(
            shadow_decision_id=decision.id,
            candidate_id=cid,
            phase="EXIT",
            observed_at=observed_at,
            status="NOT_EXECUTABLE" if missing else "OBSERVED",
            failure_category=category,
            failure_detail=detail,
            net_executable_value=None if missing else net,
            market_data_quality=(
                next(iter(qualities))
                if len(qualities) == 1
                else ("mixed:" + ",".join(sorted(qualities)) if qualities else None)
            ),
            source_provider="ibkr_tws",
            earliest_leg_observed_at=min(stamps) if stamps else None,
            latest_leg_observed_at=max(stamps) if stamps else None,
            max_leg_timestamp_skew_seconds=(
                Decimal(str((max(stamps) - min(stamps)).total_seconds()))
                if len(stamps) > 1
                else (Decimal(0) if stamps else None)
            ),
            unique_contract_count=len(
                {leg.external_contract_id for leg in legs if leg.external_contract_id}
            )
            or None,
            legs_json={"legs": rows, "pricing_convention": EXIT_CONVENTION},
        )
        db.add(obs)
        exit_obs[cid] = obs
    db.flush()

    # 3. One settlement per pending configuration, for its own quantity.
    for entry in pending:
        cand_exit: V4ShadowCandidateObservation | None = exit_obs.get(entry.candidate_id)
        try:
            if (
                cand_exit is None
                or cand_exit.status != "OBSERVED"
                or cand_exit.net_executable_value is None
            ):
                row = V4ShadowConfigSettlement(
                    timing_policy_version=timing_policy_version,
                    shadow_config_result_id=entry.shadow_config_result_id,
                    shadow_decision_id=decision.id,
                    candidate_observation_id=cand_exit.id if cand_exit else None,
                    configuration_key=entry.configuration_key,
                    candidate_id=entry.candidate_id,
                    status="OBSERVATION_FAILED",
                    quantity=entry.quantity,
                    standardized_capital=entry.standardized_capital,
                    capital_used=entry.capital_used,
                    entry_net_value=entry.entry_net_value,
                    entry_observed_at=entry.observed_at,
                    settled_at=observed_at,
                    pricing_convention=EXIT_CONVENTION,
                    market_data_quality=cand_exit.market_data_quality if cand_exit else None,
                    failure_category=(
                        obs.failure_category if obs else "SETTLEMENT_OBSERVATION_FAILED"
                    ),
                    failure_detail=(
                        obs.failure_detail if obs else "no exit observation for this candidate"
                    ),
                )
                summary.failed += 1
                summary.by_configuration[entry.configuration_key] = "SETTLEMENT_FAILED"
            else:
                exit_value = cand_exit.net_executable_value * entry.quantity
                entry_value = entry.entry_net_value or Decimal(0)
                realized = exit_value - entry_value
                row = V4ShadowConfigSettlement(
                    timing_policy_version=timing_policy_version,
                    shadow_config_result_id=entry.shadow_config_result_id,
                    shadow_decision_id=decision.id,
                    candidate_observation_id=cand_exit.id,
                    configuration_key=entry.configuration_key,
                    candidate_id=entry.candidate_id,
                    status="SETTLED",
                    quantity=entry.quantity,
                    standardized_capital=entry.standardized_capital,
                    capital_used=entry.capital_used,
                    entry_net_value=entry_value,
                    exit_net_value=exit_value,
                    realized_pnl=realized,
                    return_on_standardized_capital=realized / entry.standardized_capital,
                    entry_observed_at=entry.observed_at,
                    settled_at=observed_at,
                    pricing_convention=EXIT_CONVENTION,
                    market_data_quality=cand_exit.market_data_quality,
                )
                summary.settled += 1
                summary.by_configuration[entry.configuration_key] = "SETTLED"
            db.add(row)
        except Exception as exc:  # noqa: BLE001 -- Section 16: isolate per configuration
            log.error("config settlement failed for %s", entry.configuration_key, exc_info=True)
            summary.failed += 1
            summary.by_configuration[entry.configuration_key] = f"FAILED:{type(exc).__name__}"
    db.flush()

    # 4. Derived decision-level settlement for the unconstrained rank #1, from
    #    the SAME exit observation (no additional quote), so the event-level
    #    reference read models stay populated. Idempotent.
    top = decision.rank_1_candidate_id
    if (
        top
        and top in exit_obs
        and not db.query(V4ShadowSettlement).filter_by(shadow_decision_id=decision.id).first()
    ):
        top_obs = exit_obs[top]
        entry_row = next((e for e in entries if e.candidate_id == top), None)
        if (
            top_obs.status == "OBSERVED"
            and top_obs.net_executable_value is not None
            and entry_row is not None
        ):
            per_unit_entry = (entry_row.entry_net_value or Decimal(0)) / entry_row.quantity
            realized = top_obs.net_executable_value - per_unit_entry
            db.add(
                V4ShadowSettlement(
                    timing_policy_version=timing_policy_version,
                    shadow_decision_id=decision.id,
                    settled_at=observed_at,
                    status="SETTLED",
                    entry_net_value=per_unit_entry,
                    exit_net_value=top_obs.net_executable_value,
                    realized_pnl=realized,
                    return_on_standardized_capital=realized / Decimal("2000"),
                    market_data_quality=top_obs.market_data_quality,
                )
            )
    db.add(
        V4ShadowRunEvent(
            shadow_decision_id=decision.id,
            earnings_calendar_event_id=decision.earnings_calendar_event_id,
            ticker=decision.ticker,
            occurred_at=observed_at,
            stage="cohort_settlement",
            category="OK" if summary.failed == 0 else "SETTLEMENT_OBSERVATION_FAILED",
            retryable=False,
            message=(
                f"settled {summary.settled}, failed {summary.failed} across "
            f"{summary.unique_candidates} "
                f"unique candidate(s), {summary.unique_contracts} unique contract(s), "
                f"{summary.quote_calls} quote call(s)"
            ),
        )
    )
    db.flush()
    return summary


SETTLEMENT_WINDOW_MISSED = "SETTLEMENT_WINDOW_MISSED"


def fail_missed_settlement_window(
    db: Session,
    *,
    decision: V4ShadowDecision,
    observed_at: datetime,
    detail: str,
    timing_policy_version: str = V4_ACTIVE_TIMING_POLICY.version,
) -> int:
    """Closes every still-pending configuration of ``decision`` as a
    terminal OBSERVATION_FAILED settlement with SETTLEMENT_WINDOW_MISSED.

    Why this exists: V3's exit capture refuses a capture outside its legal
    window (benchmark_exit_capture, LATE_CUTOFF_GRACE) and records that
    refusal as evidence. The V4 cohort mirrors it -- a position whose
    window was missed is never "settled" later with a quote from a
    different moment, and never left WAITING_SETTLEMENT forever. Nothing
    is quoted here: this touches no provider. Idempotent: configurations
    that already have a settlement row are untouched. Returns the number
    of configurations closed."""
    entries = (
        db.query(V4ShadowConfigEntry)
        .filter_by(shadow_decision_id=decision.id, status="OBSERVED")
        .all()
    )
    already = {
        s.shadow_config_result_id
        for s in db.query(V4ShadowConfigSettlement).filter_by(shadow_decision_id=decision.id)
    }
    pending = [e for e in entries if e.shadow_config_result_id not in already]
    for entry in pending:
        db.add(
            V4ShadowConfigSettlement(
                timing_policy_version=timing_policy_version,
                shadow_config_result_id=entry.shadow_config_result_id,
                shadow_decision_id=decision.id,
                candidate_observation_id=None,
                configuration_key=entry.configuration_key,
                candidate_id=entry.candidate_id,
                status="OBSERVATION_FAILED",
                quantity=entry.quantity,
                standardized_capital=entry.standardized_capital,
                capital_used=entry.capital_used,
                entry_net_value=entry.entry_net_value,
                entry_observed_at=entry.observed_at,
                settled_at=observed_at,
                pricing_convention=EXIT_CONVENTION,
                market_data_quality=None,
                failure_category=SETTLEMENT_WINDOW_MISSED,
                failure_detail=detail,
            )
        )
    if pending:
        db.add(
            V4ShadowRunEvent(
                shadow_decision_id=decision.id,
                earnings_calendar_event_id=decision.earnings_calendar_event_id,
                ticker=decision.ticker,
                occurred_at=observed_at,
                stage="cohort_settlement",
                category=SETTLEMENT_WINDOW_MISSED,
                retryable=False,
                message=f"{len(pending)} configuration(s) closed unsettled: {detail}",
            )
        )
        db.flush()
    return len(pending)
