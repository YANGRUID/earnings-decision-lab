"""V4.2 CHALLENGER end-of-day settlement recovery.

Parity, not privilege. A challenger position stranded by an empty book at
15:30 is recovered under exactly the hierarchy the control released on
2026-09-04, imported from ``services/v4_settlement_fallback.py`` rather than
reimplemented:

  1. EXECUTABLE_BID / EXECUTABLE_ASK   -- a real required-side quote captured
     at or before the close. A price the scheduled window already captured
     wins over a fresh requote: it is the earlier, more honest observation.
  2. MARKET_CLOSE_FALLBACK             -- that contract's own same-session
     closing mark. Explicitly not a fill, and labelled so.
  3. EXPIRATION_INTRINSIC_AT_CLOSE     -- only for a contract expiring ON the
     settlement date, against the official underlying close.

A contract that is NOT expiring that day never reaches step 3 and is never
written down to zero merely because nobody was bidding: it stays unresolved
until a real closing mark exists. That rule is the product owner's and applies
to the challenger identically.

Append-only. The original failed settlement is never rewritten or deleted; a
recovery is a NEW row that points back at the attempt it supersedes, and the
partial unique index guarantees only one of them can be the settlement of
record.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from models.v4_2_challenger import (
    CHALLENGER_EXIT_CONVENTION,
    V42ChallengerCandidateObservation,
    V42ChallengerConfigEntry,
    V42ChallengerConfigSettlement,
    V42ChallengerDecision,
)
from providers.types import KnownContract
from services.v4_settlement_fallback import resolve_leg_exit_price
from services.v4_settlement_quality import settlement_grade

log = logging.getLogger("services.v4_2_challenger_recovery")

RECOVERY_PHASE = "EXIT_EOD"
RECOVERY_PROVENANCE = "EOD_SETTLEMENT_FALLBACK"
RECOVERY_UNRESOLVED = "MARKET_DATA_UNAVAILABLE_AFTER_EOD_FALLBACK"


@dataclass
class ChallengerRecoverySummary:
    session_date: date
    dry_run: bool = True
    candidates_considered: int = 0
    unique_contracts: int = 0
    quote_calls: int = 0
    close_lookups: int = 0
    settled: int = 0
    unresolved: int = 0
    rows: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _pending_entries(db: Session, session_date: date) -> list[V42ChallengerConfigEntry]:
    """Challenger positions observed at entry, with a failed settlement attempt
    on this session and no settlement of record."""
    settled_ids = {
        r[0]
        for r in db.query(V42ChallengerConfigSettlement.challenger_config_result_id).filter(
            V42ChallengerConfigSettlement.status == "SETTLED"
        )
    }
    attempted_ids = {
        r[0]
        for r in db.query(V42ChallengerConfigSettlement.challenger_config_result_id).filter(
            V42ChallengerConfigSettlement.status != "SETTLED"
        )
    }
    due = attempted_ids - settled_ids
    if not due:
        return []
    return (
        db.query(V42ChallengerConfigEntry)
        .filter(V42ChallengerConfigEntry.challenger_config_result_id.in_(due))
        .filter(V42ChallengerConfigEntry.status == "OBSERVED")
        .order_by(V42ChallengerConfigEntry.id)
        .all()
    )


def recover_challenger_settlements(
    db: Session,
    *,
    provider: Any,
    session_date: date,
    now: datetime | None = None,
    dry_run: bool = True,
) -> ChallengerRecoverySummary:
    """Recover stranded challenger positions under the control's own hierarchy.

    Never raises: every failure becomes a note on the summary.
    """
    now = now or datetime.now(UTC)
    summary = ChallengerRecoverySummary(session_date=session_date, dry_run=dry_run)

    pending = _pending_entries(db, session_date)
    if not pending:
        summary.notes.append("no unsettled challenger configurations")
        return summary

    by_candidate: dict[tuple[int, str], list[dict]] = {}
    entry_by_key: dict[tuple[int, str], V42ChallengerConfigEntry] = {}
    for entry in pending:
        key = (entry.challenger_decision_id, entry.candidate_id)
        if key not in by_candidate:
            by_candidate[key] = [
                leg
                for leg in ((entry.frozen_legs_json or {}).get("legs") or [])
                if isinstance(leg, dict)
            ]
            entry_by_key[key] = entry
    summary.candidates_considered = len(by_candidate)

    decisions: dict[int, V42ChallengerDecision] = {}
    for decision_id, _cid in by_candidate:
        if decision_id not in decisions:
            found = db.get(V42ChallengerDecision, decision_id)
            if found is not None:
                decisions[decision_id] = found

    # Rule 1 input: whatever the 15:30 EXIT observation already captured.
    prior_by_key: dict[tuple[int, str], dict[int, dict]] = {}
    for key in by_candidate:
        prior = (
            db.query(V42ChallengerCandidateObservation)
            .filter_by(challenger_decision_id=key[0], candidate_id=key[1], phase="EXIT")
            .one_or_none()
        )
        rows_json = (prior.legs_json or {}).get("legs", []) if prior is not None else []
        prior_by_key[key] = {
            int(r["leg_index"]): r
            for r in rows_json
            if isinstance(r, dict) and r.get("leg_index") is not None
        }

    unique_conids: dict[str, tuple[int, str]] = {}
    for key, legs in by_candidate.items():
        for leg in legs:
            conid = leg.get("external_contract_id")
            if conid:
                unique_conids.setdefault(str(conid), key)
    summary.unique_contracts = len(unique_conids)

    quotes: dict[str, Any] = {}
    closes: dict[str, Decimal | None] = {}
    close_sources: dict[str, str | None] = {}
    for conid, key in unique_conids.items():
        entry = entry_by_key[key]
        decision = decisions.get(key[0])
        if decision is None or entry.expiration is None:
            continue
        matching: dict | None = next(
            (
                candidate_leg
                for candidate_leg in by_candidate[key]
                if str(candidate_leg.get("external_contract_id")) == conid
            ),
            None,
        )
        if matching is None:
            continue
        try:
            fetched = provider.get_quotes_for_known_contracts(
                decision.ticker,
                [
                    KnownContract(
                        strike=Decimal(str(matching["strike"])),
                        option_type=str(matching["right"]),
                        external_contract_id=conid,
                        action=str(matching["action"]),
                    )
                ],
                entry.expiration,
                now,
            )
            summary.quote_calls += 1
            if fetched:
                quotes[conid] = fetched[0]
        except Exception as exc:  # noqa: BLE001 -- one contract must not stop the rest
            summary.notes.append(f"quote failed for conId {conid}: {type(exc).__name__}: {exc}")
        try:
            value, source = provider.get_session_close_with_source(int(conid), session_date)
            closes[conid] = value
            close_sources[conid] = source
            summary.close_lookups += 1
        except Exception as exc:  # noqa: BLE001
            closes[conid] = None
            summary.notes.append(f"close failed for conId {conid}: {type(exc).__name__}: {exc}")

    underlying_closes: dict[str, Decimal | None] = {}
    for key in by_candidate:
        decision = decisions.get(key[0])
        entry = entry_by_key[key]
        if decision is None or entry.expiration != session_date:
            continue
        if decision.ticker in underlying_closes:
            continue
        try:
            underlying_closes[decision.ticker] = provider.get_underlying_session_close(
                decision.ticker, session_date
            )
        except Exception as exc:  # noqa: BLE001
            underlying_closes[decision.ticker] = None
            summary.notes.append(
                f"underlying close failed for {decision.ticker}: {type(exc).__name__}: {exc}"
            )

    net_by_key: dict[tuple[int, str], Decimal | None] = {}
    method_by_key: dict[tuple[int, str], str | None] = {}
    observations: dict[tuple[int, str], V42ChallengerCandidateObservation | None] = {}
    for key, legs in by_candidate.items():
        entry = entry_by_key[key]
        decision = decisions.get(key[0])
        ticker = decision.ticker if decision is not None else "unknown"
        expires_today = entry.expiration == session_date
        net = Decimal(0)
        unresolved: list[int] = []
        sources: set[str] = set()
        qualities: set[str] = set()
        rows: list[dict] = []
        for leg in legs:
            conid = str(leg.get("external_contract_id") or "")
            quote = quotes.get(conid)
            action = str(leg.get("action"))
            side = "bid" if action == "buy" else "ask"
            prior_row = prior_by_key.get(key, {}).get(int(leg.get("leg_index", 0)), {})
            prior_price = prior_row.get("price")
            captured = None if prior_price is None else Decimal(str(prior_price))
            fresh = getattr(quote, side, None) if quote is not None else None
            executable = captured if captured is not None else fresh
            quality = (
                prior_row.get("market_data_quality")
                if captured is not None
                else (getattr(quote, "market_data_quality", None) if quote is not None else None)
            )
            resolution = resolve_leg_exit_price(
                action=action,
                right=str(leg.get("right")),
                strike=Decimal(str(leg.get("strike"))),
                executable_price=executable,
                session_close=closes.get(conid),
                underlying_close=underlying_closes.get(ticker),
                expires_on_settlement_date=bool(expires_today),
                book_empty=(
                    getattr(quote, f"{side}_book_empty", None) if quote is not None else None
                ),
                market_data_quality=quality,
            )
            if resolution.is_executable:
                resolution.provenance["executable_source"] = (
                    "scheduled_window_capture" if captured is not None else "recovery_requote"
                )
            if quality:
                qualities.add(quality)
            if resolution.resolved and resolution.price is not None:
                sign = Decimal(1) if action == "buy" else Decimal(-1)
                multiplier = Decimal(str(leg.get("multiplier") or "100"))
                net += sign * resolution.price * Decimal(str(leg.get("quantity") or 1)) * multiplier
                if resolution.pricing_source:
                    sources.add(resolution.pricing_source)
            else:
                unresolved.append(int(leg.get("leg_index", 0)))
            rows.append(
                {
                    "leg_index": leg.get("leg_index"),
                    "action": action,
                    "right": leg.get("right"),
                    "strike": leg.get("strike"),
                    "external_contract_id": leg.get("external_contract_id"),
                    "required_side": resolution.required_side,
                    "price": None if resolution.price is None else str(resolution.price),
                    "pricing_source": resolution.pricing_source,
                    "unresolved_reason": resolution.unresolved_reason,
                    "original_required_side_state": prior_row.get("required_side_state"),
                    "option_session_close": (
                        None if closes.get(conid) is None else str(closes.get(conid))
                    ),
                    "option_session_close_source": close_sources.get(conid),
                    "underlying_session_close": (
                        None
                        if underlying_closes.get(ticker) is None
                        else str(underlying_closes.get(ticker))
                    ),
                    "market_data_quality": quality,
                    "provenance": resolution.provenance,
                    "observed_at": now.isoformat(),
                }
            )
        net_by_key[key] = None if unresolved else net
        method = "+".join(sorted(sources)) if sources else None
        method_by_key[key] = method

        obs = None
        if not dry_run:
            existing = (
                db.query(V42ChallengerCandidateObservation)
                .filter_by(challenger_decision_id=key[0], candidate_id=key[1], phase=RECOVERY_PHASE)
                .one_or_none()
            )
            obs = existing or V42ChallengerCandidateObservation(
                challenger_decision_id=key[0],
                candidate_id=key[1],
                phase=RECOVERY_PHASE,
                observed_at=now,
                status="NOT_EXECUTABLE" if unresolved else "OBSERVED",
                failure_category=RECOVERY_UNRESOLVED if unresolved else None,
                failure_detail=(
                    "required exit side unavailable and no permitted end-of-day fallback "
                    f"applied on leg(s) {unresolved}"
                    if unresolved
                    else None
                ),
                net_executable_value=None if unresolved else net,
                pricing_convention=CHALLENGER_EXIT_CONVENTION,
                pricing_method=method,
                market_data_quality=(
                    next(iter(qualities))
                    if len(qualities) == 1
                    else ("mixed:" + ",".join(sorted(qualities)) if qualities else None)
                ),
                source_provider="ibkr_tws",
                unique_contract_count=len(
                    {
                        str(leg.get("external_contract_id"))
                        for leg in legs
                        if leg.get("external_contract_id")
                    }
                )
                or None,
                legs_json={
                    "legs": rows,
                    "pricing_convention": method or RECOVERY_UNRESOLVED,
                    "recovery_provenance": RECOVERY_PROVENANCE,
                    "session_date": session_date.isoformat(),
                },
            )
            if existing is None:
                db.add(obs)
        observations[key] = obs
    if not dry_run:
        db.flush()

    # One NEW settlement row per configuration, superseding its failed attempt.
    for entry in pending:
        key = (entry.challenger_decision_id, entry.candidate_id)
        net_exit = net_by_key.get(key)
        obs = observations.get(key)
        prior_failure = (
            db.query(V42ChallengerConfigSettlement)
            .filter_by(challenger_config_result_id=entry.challenger_config_result_id)
            .order_by(V42ChallengerConfigSettlement.id.desc())
            .first()
        )
        record = {
            "configuration_key": entry.configuration_key,
            "candidate_id": entry.candidate_id,
            "pricing_method": method_by_key.get(key),
            "supersedes_settlement_id": prior_failure.id if prior_failure is not None else None,
        }
        if net_exit is None:
            summary.unresolved += 1
            record["status"] = "UNRESOLVED"
            summary.rows.append(record)
            continue
        exit_value = net_exit * entry.quantity
        entry_value = entry.entry_net_value or Decimal(0)
        realized = exit_value - entry_value
        capital_used = entry.capital_used or Decimal(0)
        record.update(
            {
                "status": "SETTLED",
                "exit_net_value": str(exit_value),
                "realized_pnl": str(realized),
            }
        )
        summary.settled += 1
        summary.rows.append(record)
        if dry_run:
            continue
        row = V42ChallengerConfigSettlement(
            challenger_config_result_id=entry.challenger_config_result_id,
            challenger_decision_id=entry.challenger_decision_id,
            challenger_config_entry_id=entry.id,
            candidate_observation_id=obs.id if obs is not None else None,
            configuration_key=entry.configuration_key,
            candidate_id=entry.candidate_id,
            status="SETTLED",
            quantity=entry.quantity,
            standardized_capital=entry.standardized_capital,
            capital_used=entry.capital_used,
            entry_net_value=entry_value,
            exit_net_value=exit_value,
            realized_pnl=realized,
            return_on_standardized_capital=(
                realized / entry.standardized_capital if entry.standardized_capital else None
            ),
            return_on_capital_used=(realized / capital_used if capital_used > 0 else None),
            entry_observed_at=entry.observed_at,
            settled_at=now,
            pricing_convention=CHALLENGER_EXIT_CONVENTION,
            pricing_method=method_by_key.get(key),
            recovery_provenance=RECOVERY_PROVENANCE,
            supersedes_settlement_id=(prior_failure.id if prior_failure is not None else None),
            market_data_quality=obs.market_data_quality if obs is not None else None,
            timing_policy_version=entry.timing_policy_version,
        )
        row.settlement_grade = settlement_grade(row)
        db.add(row)
    if not dry_run:
        db.flush()
    return summary


__all__ = [
    "RECOVERY_PHASE",
    "RECOVERY_PROVENANCE",
    "ChallengerRecoverySummary",
    "recover_challenger_settlements",
]
