"""V4.2 CHALLENGER entry evidence -- the frozen hypothetical position.

Pure over already-frozen data. No provider is reached from this module, and
that is the whole point of where it sits in the lifecycle: the challenger
evaluates the CONTROL's own frozen candidates, so the executable prices it
needs at entry were already acquired, once, by the control's single quote
sweep. The challenger's entry therefore costs ZERO additional market-data
requests -- not "few", zero -- and the count is written on the observation
row rather than asserted in a docstring.

The shape mirrors the control's, deliberately:

    ONE candidate observation per UNIQUE selected candidate
                    |
        +-----------+-----------+-----------+ ...
        |           |           |
    2K Cons     2K Mod      10K Aggr        (each its own quantity)

Six configurations that select the same candidate share ONE observation.
Configurations that select genuinely different candidates get one observation
each, and the contracts underneath them are deduplicated across all of them.

Executable convention, identical to the control's and to V4.1's released
methodology: opening a LONG leg pays the ASK, opening a SHORT leg receives the
BID. No midpoint, no last price, no model price, no historical substitution.
A missing required side makes the observation NOT_EXECUTABLE and every
configuration holding that candidate ENTRY_FAILED -- and only those.

A NO_ACTION configuration gets no entry row at all. Not a row with quantity
zero: no row. There is nothing to settle, so no settlement and no P&L can ever
attach to a configuration that declined.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from analytics.decision.v4_2_viability import VIABILITY_GATE_VERSION
from analytics.decision.v4_configurations import (
    V4_CONFIGURATION_VERSION,
    get_configuration,
    size_configuration_position,
)
from analytics.decision_timing_policy import V4_ACTIVE_TIMING_POLICY
from analytics.options.payoff import Action, OptionLeg, analyze
from models.enums import OptionType
from models.v4_2_challenger import (
    CHALLENGER_ENTRY_CONVENTION,
    V42ChallengerCandidateObservation,
    V42ChallengerConfigEntry,
    V42ChallengerConfigResult,
    V42ChallengerDecision,
)
from models.v4_shadow import V4ShadowCandidate, V4ShadowCandidateLeg
from services.v4_settlement_fallback import (
    PRICING_EXECUTABLE_ASK,
    PRICING_EXECUTABLE_BID,
)

log = logging.getLogger("services.v4_2_challenger_entry")

ENTRY_STATUS_OBSERVED = "OBSERVED"
ENTRY_STATUS_NOT_EXECUTABLE = "NOT_EXECUTABLE"
ENTRY_STATUS_NO_ACTION = "NO_ACTION"
ENTRY_FAILURE_REQUIRED_SIDE = "REQUIRED_SIDE_QUOTE_MISSING"


@dataclass
class ChallengerEntrySummary:
    """What the entry freeze actually did, in numbers that can be checked."""

    unique_candidates: list[str] = field(default_factory=list)
    unique_contracts: int = 0
    contracts_shared_with_control: int = 0
    market_data_requests_issued: int = 0
    entries_observed: int = 0
    entries_failed: int = 0
    no_action: int = 0
    by_configuration: dict[str, str] = field(default_factory=dict)
    already_frozen: int = 0

    @property
    def status(self) -> str:
        if self.entries_observed:
            return "ENTRY_CAPTURED" if not self.entries_failed else "PARTIALLY_CAPTURED"
        if self.entries_failed:
            return "ENTRY_FAILED"
        return "NO_ACTION"


def _entry_side_price(leg: V4ShadowCandidateLeg) -> tuple[str, Decimal | None, str]:
    """The ONE side this leg must be opened on, and what the frozen quote
    actually said about it. Returns (side, price, pricing_source)."""
    if leg.action == "buy":
        return "ask", leg.ask, PRICING_EXECUTABLE_ASK
    return "bid", leg.bid, PRICING_EXECUTABLE_BID


def _leg_row(leg: V4ShadowCandidateLeg, side: str, price: Decimal | None) -> dict:
    """One leg's complete entry evidence: what was needed, what was seen, and
    the liquidity/provenance around it (Section 9). Sizes and open interest
    are recorded as observed -- NULL stays NULL and never becomes a zero."""
    return {
        "leg_index": leg.leg_index,
        "action": leg.action,
        "right": leg.right,
        "strike": str(leg.strike),
        "quantity": leg.quantity,
        "multiplier": str(leg.multiplier) if leg.multiplier is not None else None,
        "external_contract_id": leg.external_contract_id,
        "expiration": None,
        "required_side": side,
        "price": None if price is None else str(price),
        "pricing_source": (
            None
            if price is None
            else (PRICING_EXECUTABLE_ASK if side == "ask" else PRICING_EXECUTABLE_BID)
        ),
        "bid": None if leg.bid is None else str(leg.bid),
        "ask": None if leg.ask is None else str(leg.ask),
        "bid_size": leg.bid_size,
        "ask_size": leg.ask_size,
        "volume": leg.volume,
        "open_interest": leg.open_interest,
        "implied_volatility": (
            None if leg.implied_volatility is None else str(leg.implied_volatility)
        ),
        "delta": None if leg.delta is None else str(leg.delta),
        "market_data_quality": leg.market_data_quality,
        "source": "control_frozen_entry_quote",
    }


def freeze_challenger_entries(
    db: Session,
    *,
    challenger: V42ChallengerDecision,
    observed_at: datetime | None = None,
    settlement_date: Any = None,
) -> ChallengerEntrySummary:
    """Freeze one challenger decision's entry evidence.

    Idempotent: a decision whose configurations already have entries is left
    exactly as it is (``already_frozen``), never rewritten. Never raises --
    a challenger entry failure is recorded as challenger evidence, because a
    challenger fault must not be able to reach the control's transaction.
    """
    summary = ChallengerEntrySummary()
    observed_at = observed_at or challenger.observed_at

    config_rows = (
        db.query(V42ChallengerConfigResult)
        .filter_by(challenger_decision_id=challenger.id)
        .order_by(V42ChallengerConfigResult.id)
        .all()
    )
    existing = {
        e.challenger_config_result_id
        for e in db.query(V42ChallengerConfigEntry).filter_by(challenger_decision_id=challenger.id)
    }

    actionable = [
        r
        for r in config_rows
        if r.status == "RANKED" and r.selected_candidate_id and r.id not in existing
    ]
    summary.already_frozen = len([r for r in config_rows if r.id in existing])
    summary.no_action = len([r for r in config_rows if r.status != "RANKED"])
    for row in config_rows:
        if row.status != "RANKED":
            summary.by_configuration[row.configuration_key] = ENTRY_STATUS_NO_ACTION
    if not actionable:
        return summary

    # ---- 1. ONE observation per UNIQUE selected candidate ------------------
    selected_ids: list[str] = sorted(
        {r.selected_candidate_id for r in actionable if r.selected_candidate_id}
    )
    control_candidates = {
        c.candidate_id: c
        for c in db.query(V4ShadowCandidate)
        .filter_by(shadow_decision_id=challenger.shadow_decision_id)
        .filter(V4ShadowCandidate.candidate_id.in_(selected_ids))
    }
    legs_by_candidate: dict[str, list[V4ShadowCandidateLeg]] = {}
    if control_candidates:
        for leg in (
            db.query(V4ShadowCandidateLeg)
            .filter(
                V4ShadowCandidateLeg.shadow_candidate_id.in_(
                    [c.id for c in control_candidates.values()]
                )
            )
            .order_by(V4ShadowCandidateLeg.leg_index)
        ):
            cid = next(
                (k for k, v in control_candidates.items() if v.id == leg.shadow_candidate_id), None
            )
            if cid is not None:
                legs_by_candidate.setdefault(cid, []).append(leg)

    observations: dict[str, V42ChallengerCandidateObservation] = {}
    all_contracts: set[str] = set()
    for candidate_id in selected_ids:
        legs = legs_by_candidate.get(candidate_id) or []
        control = control_candidates.get(candidate_id)
        net = Decimal(0)
        missing: list[int] = []
        rows: list[dict] = []
        qualities: set[str] = set()
        stamps: list[datetime] = []
        contracts: set[str] = set()
        for leg in legs:
            side, price, _source = _entry_side_price(leg)
            if price is None:
                missing.append(leg.leg_index)
            else:
                sign = Decimal(1) if leg.action == "buy" else Decimal(-1)
                net += sign * price * Decimal(leg.quantity) * (leg.multiplier or Decimal("100"))
            leg_row = _leg_row(leg, side, price)
            if control is not None:
                leg_row["expiration"] = control.expiration.isoformat()
            rows.append(leg_row)
            if leg.market_data_quality:
                qualities.add(leg.market_data_quality)
            stamp = getattr(leg, "retrieved_at", None)
            if stamp is not None:
                stamps.append(stamp)
            if leg.external_contract_id:
                contracts.add(str(leg.external_contract_id))
        all_contracts |= contracts

        if not legs:
            missing = [0]
            detail: str | None = (
                f"candidate {candidate_id} has no frozen control legs; "
                "no entry evidence can be derived without inventing quotes"
            )
        elif missing:
            detail = (
                f"required entry side missing on leg(s) {missing} -- no midpoint, "
                "last-price, model or historical substitution is permitted"
            )
        else:
            detail = None

        obs = V42ChallengerCandidateObservation(
            challenger_decision_id=challenger.id,
            candidate_id=candidate_id,
            phase="ENTRY",
            observed_at=observed_at,
            status=ENTRY_STATUS_NOT_EXECUTABLE if missing else ENTRY_STATUS_OBSERVED,
            failure_category=ENTRY_FAILURE_REQUIRED_SIDE if missing else None,
            failure_detail=detail,
            net_executable_value=None if missing else net,
            pricing_convention=CHALLENGER_ENTRY_CONVENTION,
            pricing_method=None if missing else "EXECUTABLE_BID_ASK",
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
            unique_contract_count=len(contracts) or None,
            # Every contract came from the control's own frozen observation:
            # the challenger reused all of them and quoted none itself.
            contracts_shared_with_control=len(contracts),
            legs_json={"legs": rows, "pricing_convention": CHALLENGER_ENTRY_CONVENTION},
        )
        db.add(obs)
        observations[candidate_id] = obs
        summary.unique_candidates.append(candidate_id)
    db.flush()
    summary.unique_contracts = len(all_contracts)
    summary.contracts_shared_with_control = len(all_contracts)
    summary.market_data_requests_issued = 0

    # ---- 2. ONE entry per actionable configuration, sized independently ----
    for config_row in actionable:
        selected = config_row.selected_candidate_id
        if not selected:  # pragma: no cover -- defensive
            continue
        held = observations.get(selected)
        control = control_candidates.get(selected)
        if held is None:  # pragma: no cover -- defensive
            continue
        try:
            configuration = get_configuration(config_row.configuration_key)
            per_cash = (
                Decimal(str(control.entry_cash_required))
                if control is not None and control.entry_cash_required is not None
                else Decimal(0)
            )
            per_risk = max_defined_risk_from_legs(legs_by_candidate.get(selected) or [])
            if per_risk is None:
                # An undefined-risk structure cannot be checked against a
                # fixed risk cap. The control refuses to size one rather than
                # guessing, and the challenger must refuse identically.
                per_risk = per_cash
            position = size_configuration_position(
                configuration,
                candidate_id=selected,
                per_contract_entry_cash=per_cash,
                per_contract_max_risk=per_risk,
            )
            observed = held.status == ENTRY_STATUS_OBSERVED
            legs_json = held.legs_json or {}
            entry = V42ChallengerConfigEntry(
                challenger_config_result_id=config_row.id,
                challenger_decision_id=challenger.id,
                candidate_observation_id=held.id,
                configuration_key=config_row.configuration_key,
                candidate_id=selected,
                status=ENTRY_STATUS_OBSERVED if observed else ENTRY_STATUS_NOT_EXECUTABLE,
                quantity=position.quantity,
                standardized_capital=position.standardized_capital,
                capital_used=position.capital_used,
                max_risk_per_contract=position.per_contract_max_risk,
                max_risk_used=position.max_risk_used,
                entry_net_value=(
                    (held.net_executable_value or Decimal(0)) * position.quantity
                    if observed
                    else None
                ),
                pricing_convention=CHALLENGER_ENTRY_CONVENTION,
                observed_at=observed_at,
                market_data_quality=held.market_data_quality,
                failure_category=None if observed else held.failure_category,
                failure_detail=None if observed else held.failure_detail,
                # The exact contracts, frozen. Settlement resolves against
                # these conIds and can never re-select a strike.
                frozen_legs_json={
                    "legs": legs_json.get("legs", []),
                    "quantity": position.quantity,
                    "multiplier": "100",
                },
                expiration=control.expiration if control is not None else None,
                expiry_ladder_position=_ladder_position(db, challenger.id, selected),
                entry_dte=(
                    (control.expiration - observed_at.date()).days if control is not None else None
                ),
                dte_at_settlement=(
                    (control.expiration - settlement_date).days
                    if control is not None and settlement_date is not None
                    else None
                ),
                timing_policy_version=V4_ACTIVE_TIMING_POLICY.version,
                methodology_version=VIABILITY_GATE_VERSION,
                configuration_version=V4_CONFIGURATION_VERSION,
            )
            db.add(entry)
            summary.by_configuration[config_row.configuration_key] = entry.status
            if observed:
                summary.entries_observed += 1
            else:
                summary.entries_failed += 1
        except Exception as exc:  # noqa: BLE001 -- one configuration never fails five
            log.error("challenger entry freeze failed for %s", row.configuration_key, exc_info=True)
            summary.by_configuration[config_row.configuration_key] = f"FAILED:{type(exc).__name__}"
            summary.entries_failed += 1
    db.flush()
    return summary


def max_defined_risk_from_legs(legs: list[V4ShadowCandidateLeg]) -> Decimal | None:
    """Maximum loss of ONE unit, computed from the persisted legs.

    Identical in definition to the control's ``max_defined_risk``: the same
    payoff analysis, over the same executable entry premiums (buy pays ASK,
    sell receives BID). It reads the frozen leg rows rather than a live
    RankableCandidate because at settlement time the in-memory candidate is
    long gone -- the evidence is all that remains, which is the point.

    ``None`` means unbounded below or unpriceable, never a convenient zero.
    """
    payoff_legs: list[OptionLeg] = []
    for leg in legs:
        premium = leg.ask if leg.action == "buy" else leg.bid
        if premium is None:
            return None
        try:
            payoff_legs.append(
                OptionLeg(
                    option_type=OptionType(leg.right),
                    action=Action.BUY if leg.action == "buy" else Action.SELL,
                    strike=leg.strike,
                    premium=premium,
                    quantity=leg.quantity,
                )
            )
        except (ValueError, KeyError):
            return None
    if not payoff_legs:
        return None
    analysis = analyze(payoff_legs)
    if analysis.max_loss is None:
        return None
    multiplier = legs[0].multiplier or Decimal("100")
    return analysis.max_loss * multiplier


def _ladder_position(db: Session, challenger_decision_id: int, candidate_id: str) -> int | None:
    from models.v4_2_challenger import V42ChallengerCandidate  # noqa: PLC0415

    row = (
        db.query(V42ChallengerCandidate.expiry_ladder_position)
        .filter_by(challenger_decision_id=challenger_decision_id, candidate_id=candidate_id)
        .first()
    )
    return None if row is None else row[0]


__all__ = [
    "ENTRY_STATUS_NOT_EXECUTABLE",
    "ENTRY_STATUS_NO_ACTION",
    "ENTRY_STATUS_OBSERVED",
    "ChallengerEntrySummary",
    "freeze_challenger_entries",
    "max_defined_risk_from_legs",
]
