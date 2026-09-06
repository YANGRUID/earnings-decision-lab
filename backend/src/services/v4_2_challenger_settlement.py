"""V4.2 CHALLENGER settlement -- realized forward outcomes for challenger
positions, under the control's own released methodology.

The rule this module exists to enforce is that the challenger gets NO
advantage anywhere in the exit:

  * the same T+1 15:30 ET objective, from the project's active timing policy
    -- never a separately chosen, more favourable exit instant;
  * the same executable convention -- close a LONG at the BID, a SHORT at the
    ASK, with no midpoint, last price, model price or historical substitution;
  * the same empty-book semantics -- an IBKR -1 price with size 0 is a real
    market fact (NO_BID / NO_ASK), not an unanswered request, and is recorded
    as such;
  * the same end-of-day fallback hierarchy, from
    ``services/v4_settlement_fallback.py``, with the same pricing-source
    labels and the same refusal to write a living option down to zero;
  * the same settlement-quality grading, from
    ``services/v4_settlement_quality.py``, worst-leg-wins.

A challenger that could settle on kinder terms than the control would make
every comparison between them meaningless, so none of the above is
re-implemented here -- the released modules are imported and used.

The exact frozen position settles. Contracts are resolved by the conIds
frozen at entry: no strike is re-selected, no expiration changed, no strategy
substituted and no quantity resized. The frozen legs are the position.

Quote sharing (Section 82). When the control settled a position holding the
same contract in the same window, its already-acquired quote is reused rather
than re-requested. Only genuinely challenger-only contracts become new
subscriptions, and the count of each is persisted on the observation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from analytics.decision_timing_policy import V4_ACTIVE_TIMING_POLICY
from models.v4_2_challenger import (
    CHALLENGER_EXIT_CONVENTION,
    V42ChallengerCandidateObservation,
    V42ChallengerConfigEntry,
    V42ChallengerConfigSettlement,
    V42ChallengerDecision,
)
from providers.base import OptionsDataProvider
from providers.types import KnownContract
from services.v4_settlement_fallback import (
    PRICING_EXECUTABLE_ASK,
    PRICING_EXECUTABLE_BID,
    required_exit_side,
)
from services.v4_settlement_quality import settlement_grade

log = logging.getLogger("services.v4_2_challenger_settlement")

SETTLEMENT_STATUS_SETTLED = "SETTLED"
SETTLEMENT_STATUS_FAILED = "OBSERVATION_FAILED"

# The same required-side failure taxonomy the control uses. Kept as its own
# constants rather than imported from the cohort module so that a challenger
# settlement can never accidentally acquire control-specific behaviour, but
# the STRINGS are deliberately identical so Operations can group them.
EXIT_NO_BID = "NO_BID"
EXIT_NO_ASK = "NO_ASK"
EXIT_NO_EXECUTABLE_SIDE = "NO_EXECUTABLE_SIDE"
EXIT_REQUIRED_SIDE_TIMEOUT = "REQUIRED_SIDE_TIMEOUT"
EXIT_REQUIRED_SIDE_MISSING = "REQUIRED_SIDE_QUOTE_MISSING"
SETTLEMENT_WINDOW_MISSED = "SETTLEMENT_WINDOW_MISSED"


@dataclass
class ChallengerSettlementSummary:
    unique_candidates: int = 0
    unique_contracts: int = 0
    contracts_reused_from_control: int = 0
    quote_calls: int = 0
    settled: int = 0
    failed: int = 0
    skipped_already: int = 0
    by_configuration: dict[str, str] = field(default_factory=dict)


def _required_side_state(quote: Any, side: str) -> str:
    """present | book_empty | unavailable -- what the provider actually said
    about the ONE side this leg must be closed on. The distinction matters:
    only 'unavailable' is worth retrying, and only 'book_empty' is a real
    market fact."""
    if quote is None:
        return "unavailable"
    if getattr(quote, side, None) is not None:
        return "present"
    return "book_empty" if getattr(quote, f"{side}_book_empty", None) else "unavailable"


def _failure_category(missing_rows: list[dict]) -> str:
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


def _failure_detail(missing_rows: list[dict]) -> str:
    parts = [
        f"leg {r['leg_index']} ({r['action']} {r['right']} {r['strike']}, "
        f"conId {r['external_contract_id']}) needs {r['required_side'].upper()}: "
        f"{r['required_side_state']}; bid={r['bid']} ask={r['ask']} "
        f"bid_size={r['bid_size']} ask_size={r['ask_size']} quality={r['market_data_quality']}"
        for r in missing_rows
    ]
    return (
        "required exit side missing on leg(s) "
        f"{[r['leg_index'] for r in missing_rows]} -- no midpoint, last-price, "
        "historical or intrinsic substitution is permitted at the 15:30 window. "
        + " | ".join(parts)
    )


def _frozen_legs(entry: V42ChallengerConfigEntry) -> list[dict]:
    payload = entry.frozen_legs_json or {}
    legs = payload.get("legs") or []
    return [leg for leg in legs if isinstance(leg, dict)]


def settle_challenger_decision(
    db: Session,
    *,
    provider: OptionsDataProvider,
    challenger: V42ChallengerDecision,
    observed_at: datetime,
    shared_quotes: dict[str, Any] | None = None,
    timing_policy_version: str | None = None,
) -> ChallengerSettlementSummary:
    """Settle every pending challenger configuration of one decision.

    ``shared_quotes`` maps conId -> quote and is whatever the control's own
    settlement already acquired in this window; anything found there is used
    as-is and is never re-requested.

    Never raises: a challenger settlement failure becomes challenger evidence.
    """
    summary = ChallengerSettlementSummary()
    shared_quotes = shared_quotes or {}
    policy_version = timing_policy_version or V4_ACTIVE_TIMING_POLICY.version

    entries = (
        db.query(V42ChallengerConfigEntry)
        .filter_by(challenger_decision_id=challenger.id, status="OBSERVED")
        .all()
    )
    already = {
        s.challenger_config_result_id
        for s in db.query(V42ChallengerConfigSettlement).filter_by(
            challenger_decision_id=challenger.id, status=SETTLEMENT_STATUS_SETTLED
        )
    }
    pending = [e for e in entries if e.challenger_config_result_id not in already]
    summary.skipped_already = len(entries) - len(pending)
    if not pending:
        return summary

    # ---- 1. dedupe contracts across every held candidate ------------------
    # The frozen legs ARE the position: contracts come from the entry rows'
    # own conIds, never from a fresh chain lookup that could resolve
    # differently.
    legs_by_candidate: dict[str, list[dict]] = {}
    expiration_by_candidate: dict[str, Any] = {}
    for entry in pending:
        if entry.candidate_id not in legs_by_candidate:
            legs_by_candidate[entry.candidate_id] = _frozen_legs(entry)
            expiration_by_candidate[entry.candidate_id] = entry.expiration
    summary.unique_candidates = len(legs_by_candidate)

    needed: dict[Any, dict[str, dict]] = {}
    for cid, legs in legs_by_candidate.items():
        expiration = expiration_by_candidate.get(cid)
        for leg in legs:
            conid = leg.get("external_contract_id")
            if conid:
                needed.setdefault(expiration, {})[str(conid)] = leg
    all_conids = {c for group in needed.values() for c in group}
    summary.unique_contracts = len(all_conids)

    quotes_by_conid: dict[str, Any] = {
        conid: shared_quotes[conid] for conid in all_conids if conid in shared_quotes
    }
    summary.contracts_reused_from_control = len(quotes_by_conid)

    quote_errors: dict[Any, str] = {}
    for expiration, group in needed.items():
        outstanding = {c: leg for c, leg in group.items() if c not in quotes_by_conid}
        if not outstanding or expiration is None:
            continue
        known: list[KnownContract] = [
            KnownContract(
                strike=Decimal(str(leg["strike"])),
                option_type=str(leg["right"]),
                external_contract_id=conid,
                action=str(leg["action"]),
            )
            for conid, leg in outstanding.items()
        ]
        summary.quote_calls += 1
        try:
            quotes = provider.get_quotes_for_known_contracts(
                challenger.ticker, known, expiration, observed_at
            )
        except Exception as exc:  # noqa: BLE001 -- provider boundary, isolated per group
            quote_errors[expiration] = f"{type(exc).__name__}: {exc}"
            continue
        by_key = {(str(q.strike), q.option_type): q for q in quotes}
        for conid, leg in outstanding.items():
            q = by_key.get((str(Decimal(str(leg["strike"]))), str(leg["right"])))
            if q is not None:
                quotes_by_conid[conid] = q

    # ---- 2. ONE exit observation per unique held candidate ----------------
    exit_obs: dict[str, V42ChallengerCandidateObservation] = {}
    for cid, legs in legs_by_candidate.items():
        existing = (
            db.query(V42ChallengerCandidateObservation)
            .filter_by(challenger_decision_id=challenger.id, candidate_id=cid, phase="EXIT")
            .one_or_none()
        )
        if existing is not None:
            exit_obs[cid] = existing
            continue
        net = Decimal(0)
        missing: list[int] = []
        rows: list[dict] = []
        qualities: set[str] = set()
        stamps: list[datetime] = []
        shared_here = 0
        for leg in legs:
            conid = str(leg.get("external_contract_id") or "")
            q = quotes_by_conid.get(conid)
            action = str(leg.get("action"))
            side = required_exit_side(action)
            price = (q.bid if action == "buy" else q.ask) if q is not None else None
            if price is None:
                missing.append(int(leg.get("leg_index", 0)))
            else:
                sign = Decimal(1) if action == "buy" else Decimal(-1)
                multiplier = Decimal(str(leg.get("multiplier") or "100"))
                net += sign * price * Decimal(str(leg.get("quantity") or 1)) * multiplier
            if q is not None:
                if getattr(q, "market_data_quality", None):
                    qualities.add(q.market_data_quality)
                if getattr(q, "retrieved_at", None):
                    stamps.append(q.retrieved_at)
            if conid and conid in shared_quotes:
                shared_here += 1
            rows.append(
                {
                    "leg_index": leg.get("leg_index"),
                    "action": action,
                    "right": leg.get("right"),
                    "strike": leg.get("strike"),
                    "quantity": leg.get("quantity"),
                    "external_contract_id": leg.get("external_contract_id"),
                    "required_side": side,
                    "required_side_state": _required_side_state(q, side),
                    "price": None if price is None else str(price),
                    "pricing_source": (
                        None
                        if price is None
                        else (PRICING_EXECUTABLE_BID if side == "bid" else PRICING_EXECUTABLE_ASK)
                    ),
                    "bid": None if q is None or q.bid is None else str(q.bid),
                    "ask": None if q is None or q.ask is None else str(q.ask),
                    "bid_size": getattr(q, "bid_size", None),
                    "ask_size": getattr(q, "ask_size", None),
                    "bid_book_empty": getattr(q, "bid_book_empty", None),
                    "ask_book_empty": getattr(q, "ask_book_empty", None),
                    "market_data_quality": getattr(q, "market_data_quality", None),
                    "retrieved_at": (
                        q.retrieved_at.isoformat()
                        if q is not None and getattr(q, "retrieved_at", None)
                        else None
                    ),
                    "quote_source": (
                        "reused_from_control" if conid in shared_quotes else "challenger_request"
                    ),
                }
            )
        missing_rows = [r for r in rows if r["leg_index"] in missing]
        category = _failure_category(missing_rows) if missing else None
        detail = _failure_detail(missing_rows) if missing else None
        expiration = expiration_by_candidate.get(cid)
        if expiration in quote_errors and missing:
            category = EXIT_REQUIRED_SIDE_MISSING
            detail = f"exit quote acquisition failed: {quote_errors[expiration]}"
        contract_ids = {
            str(leg.get("external_contract_id")) for leg in legs if leg.get("external_contract_id")
        }
        obs = V42ChallengerCandidateObservation(
            challenger_decision_id=challenger.id,
            candidate_id=cid,
            phase="EXIT",
            observed_at=observed_at,
            status="NOT_EXECUTABLE" if missing else "OBSERVED",
            failure_category=category,
            failure_detail=detail,
            net_executable_value=None if missing else net,
            pricing_convention=CHALLENGER_EXIT_CONVENTION,
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
            unique_contract_count=len(contract_ids) or None,
            contracts_shared_with_control=shared_here,
            legs_json={"legs": rows, "pricing_convention": CHALLENGER_EXIT_CONVENTION},
        )
        db.add(obs)
        exit_obs[cid] = obs
    db.flush()

    # ---- 3. one settlement per pending configuration ----------------------
    for entry in pending:
        held = exit_obs.get(entry.candidate_id)
        try:
            row = _settlement_row(
                entry=entry,
                challenger=challenger,
                observation=held,
                observed_at=observed_at,
                policy_version=policy_version,
            )
            db.add(row)
            if row.status == SETTLEMENT_STATUS_SETTLED:
                summary.settled += 1
                summary.by_configuration[entry.configuration_key] = SETTLEMENT_STATUS_SETTLED
            else:
                summary.failed += 1
                summary.by_configuration[entry.configuration_key] = "SETTLEMENT_FAILED"
        except Exception as exc:  # noqa: BLE001 -- isolate per configuration
            log.error("challenger settlement failed for %s", entry.configuration_key, exc_info=True)
            summary.failed += 1
            summary.by_configuration[entry.configuration_key] = f"FAILED:{type(exc).__name__}"
    db.flush()
    return summary


def _settlement_row(
    *,
    entry: V42ChallengerConfigEntry,
    challenger: V42ChallengerDecision,
    observation: V42ChallengerCandidateObservation | None,
    observed_at: datetime,
    policy_version: str,
    pricing_method: str | None = None,
    recovery_provenance: str | None = None,
    supersedes_settlement_id: int | None = None,
) -> V42ChallengerConfigSettlement:
    """One configuration's realized result for its own frozen quantity."""
    common = {
        "challenger_config_result_id": entry.challenger_config_result_id,
        "challenger_decision_id": challenger.id,
        "challenger_config_entry_id": entry.id,
        "candidate_observation_id": observation.id if observation is not None else None,
        "configuration_key": entry.configuration_key,
        "candidate_id": entry.candidate_id,
        "quantity": entry.quantity,
        "standardized_capital": entry.standardized_capital,
        "capital_used": entry.capital_used,
        "entry_net_value": entry.entry_net_value,
        "entry_observed_at": entry.observed_at,
        "settled_at": observed_at,
        "pricing_convention": CHALLENGER_EXIT_CONVENTION,
        "timing_policy_version": policy_version,
        "recovery_provenance": recovery_provenance,
        "supersedes_settlement_id": supersedes_settlement_id,
    }
    if (
        observation is None
        or observation.status != "OBSERVED"
        or observation.net_executable_value is None
    ):
        row = V42ChallengerConfigSettlement(
            status=SETTLEMENT_STATUS_FAILED,
            market_data_quality=(
                observation.market_data_quality if observation is not None else None
            ),
            failure_category=(
                observation.failure_category
                if observation is not None
                else "SETTLEMENT_OBSERVATION_FAILED"
            ),
            failure_detail=(
                observation.failure_detail
                if observation is not None
                else "no exit observation for this candidate"
            ),
            pricing_method=pricing_method,
            **common,
        )
        row.settlement_grade = settlement_grade(row)
        return row

    exit_value = observation.net_executable_value * entry.quantity
    entry_value = entry.entry_net_value or Decimal(0)
    realized = exit_value - entry_value
    capital_used = entry.capital_used or Decimal(0)
    row = V42ChallengerConfigSettlement(
        status=SETTLEMENT_STATUS_SETTLED,
        exit_net_value=exit_value,
        realized_pnl=realized,
        return_on_standardized_capital=(
            realized / entry.standardized_capital if entry.standardized_capital else None
        ),
        # Return on the capital the position actually consumed. Reported
        # alongside, never instead of, the standardized figure: they answer
        # different questions and neither one alone is the honest number.
        return_on_capital_used=(realized / capital_used if capital_used > 0 else None),
        market_data_quality=observation.market_data_quality,
        pricing_method=pricing_method or observation.pricing_method,
        **common,
    )
    row.settlement_grade = settlement_grade(row)
    return row


def fail_missed_challenger_window(
    db: Session,
    *,
    challenger: V42ChallengerDecision,
    observed_at: datetime,
    detail: str,
    timing_policy_version: str | None = None,
) -> int:
    """Close every still-pending challenger configuration as a terminal
    SETTLEMENT_WINDOW_MISSED failure.

    Mirrors the control exactly: a position whose legal window was missed is
    never settled later with a quote from a different moment, and is never
    left waiting forever either. Touches no provider.
    """
    policy_version = timing_policy_version or V4_ACTIVE_TIMING_POLICY.version
    entries = (
        db.query(V42ChallengerConfigEntry)
        .filter_by(challenger_decision_id=challenger.id, status="OBSERVED")
        .all()
    )
    already = {
        s.challenger_config_result_id
        for s in db.query(V42ChallengerConfigSettlement).filter_by(
            challenger_decision_id=challenger.id
        )
    }
    pending = [e for e in entries if e.challenger_config_result_id not in already]
    for entry in pending:
        row = V42ChallengerConfigSettlement(
            challenger_config_result_id=entry.challenger_config_result_id,
            challenger_decision_id=challenger.id,
            challenger_config_entry_id=entry.id,
            candidate_observation_id=None,
            configuration_key=entry.configuration_key,
            candidate_id=entry.candidate_id,
            status=SETTLEMENT_STATUS_FAILED,
            quantity=entry.quantity,
            standardized_capital=entry.standardized_capital,
            capital_used=entry.capital_used,
            entry_net_value=entry.entry_net_value,
            entry_observed_at=entry.observed_at,
            settled_at=observed_at,
            pricing_convention=CHALLENGER_EXIT_CONVENTION,
            timing_policy_version=policy_version,
            failure_category=SETTLEMENT_WINDOW_MISSED,
            failure_detail=detail,
        )
        row.settlement_grade = settlement_grade(row)
        db.add(row)
    if pending:
        db.flush()
    return len(pending)


__all__ = [
    "SETTLEMENT_STATUS_FAILED",
    "SETTLEMENT_STATUS_SETTLED",
    "SETTLEMENT_WINDOW_MISSED",
    "ChallengerSettlementSummary",
    "fail_missed_challenger_window",
    "settle_challenger_decision",
]
