"""Authorized end-of-day recovery for V4 positions that were due to settle
on a given session but whose scheduled attempt could not price every leg.

Scope, deliberately narrow (product owner authorization, 2026-09-04):
  * only configurations whose scheduled settlement attempt already ran on
    THIS session date and did not reach SETTLED;
  * only the exact frozen candidate, contracts, strikes, expirations,
    quantities and long/short structure those configurations already hold
    -- nothing is re-ranked, re-selected, resized or rebuilt;
  * append-only. The original failed settlement rows and their original
    missing-BID/ASK evidence are never updated, deleted or restamped; a
    recovery writes NEW rows carrying their own real timestamps.

The exit price for each leg comes from services/v4_settlement_fallback.py's
hierarchy (executable side, then the contract's own same-session closing
mark, then -- only for a contract expiring on the settlement date --
expiration intrinsic against the official underlying close). Every leg
records which of those it used.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.market_session import EASTERN
from models.enums import QuoteRequirement
from models.v4_shadow import (
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowCandidateObservation,
    V4ShadowConfigEntry,
    V4ShadowConfigSettlement,
    V4ShadowDecision,
)
from services.v4_settlement_fallback import (
    FALLBACK_PRICING_SOURCES,
    LegExitPrice,
    resolve_leg_exit_price,
)

RECOVERY_PHASE = "EXIT_EOD"
RECOVERY_PROVENANCE = "LATE_SETTLEMENT_OVERRIDE"
RECOVERY_UNRESOLVED = "MARKET_DATA_UNAVAILABLE_AFTER_EMERGENCY_RETRY"


@dataclass
class RecoveryLeg:
    leg_index: int
    action: str
    right: str
    strike: Decimal
    quantity: int
    multiplier: Decimal
    conid: str | None
    resolution: LegExitPrice | None = None


@dataclass
class RecoverySummary:
    session_date: date | None = None
    dry_run: bool = True
    candidates_considered: int = 0
    unique_contracts: int = 0
    quote_calls: int = 0
    close_lookups: int = 0
    settled: int = 0
    unresolved: int = 0
    settled_at: str | None = None
    skipped_already_settled: int = 0
    candidate_rows: list[dict] = field(default_factory=list)
    config_rows: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _eastern_date(moment: datetime) -> date:
    return moment.astimezone(EASTERN).date()


def _pending_configs(db: Session, session_date: date) -> list[V4ShadowConfigSettlement]:
    """The configurations whose scheduled attempt ran on ``session_date`` and
    is still not settled. A configuration that already has ANY settled row
    is never returned -- that is the idempotency guarantee."""
    settled_result_ids = {
        r.shadow_config_result_id
        for r in db.query(V4ShadowConfigSettlement).filter(
            V4ShadowConfigSettlement.status == "SETTLED"
        )
    }
    out: list[V4ShadowConfigSettlement] = []
    seen: set[int] = set()
    for row in (
        db.query(V4ShadowConfigSettlement)
        .filter(V4ShadowConfigSettlement.status != "SETTLED")
        .order_by(V4ShadowConfigSettlement.id)
    ):
        if row.shadow_config_result_id in settled_result_ids:
            continue
        if row.shadow_config_result_id in seen:
            continue
        if _eastern_date(row.settled_at) != session_date:
            continue
        seen.add(row.shadow_config_result_id)
        out.append(row)
    return out


def recover_due_settlements(
    db: Session,
    *,
    provider,
    session_date: date,
    now: datetime | None = None,
    dry_run: bool = True,
) -> RecoverySummary:
    now = now or datetime.now(UTC)
    summary = RecoverySummary(session_date=session_date, dry_run=dry_run)
    summary.settled_at = now.isoformat()

    pending = _pending_configs(db, session_date)
    if not pending:
        summary.notes.append("no unsettled configurations due on this session date")
        return summary

    decisions: dict[int, V4ShadowDecision] = {}
    candidates: dict[tuple[int, str], V4ShadowCandidate] = {}
    legs_by_candidate: dict[tuple[int, str], list[RecoveryLeg]] = {}

    for row in pending:
        key = (row.shadow_decision_id, row.candidate_id)
        if key in legs_by_candidate:
            continue
        decision = decisions.get(row.shadow_decision_id) or db.get(
            V4ShadowDecision, row.shadow_decision_id
        )
        if decision is None:
            continue
        decisions[row.shadow_decision_id] = decision
        candidate = (
            db.query(V4ShadowCandidate)
            .filter_by(shadow_decision_id=row.shadow_decision_id, candidate_id=row.candidate_id)
            .one_or_none()
        )
        if candidate is None:
            continue
        candidates[key] = candidate
        legs_by_candidate[key] = [
            RecoveryLeg(
                leg_index=leg.leg_index,
                action=leg.action,
                right=leg.right,
                strike=leg.strike,
                quantity=leg.quantity,
                multiplier=leg.multiplier or Decimal("100"),
                conid=leg.external_contract_id,
            )
            for leg in db.query(V4ShadowCandidateLeg)
            .filter_by(shadow_candidate_id=candidate.id)
            .order_by(V4ShadowCandidateLeg.leg_index)
        ]

    summary.candidates_considered = len(legs_by_candidate)

    # Rule 1: a real required-side quote captured at or before the close is
    # still the right price for that leg. The scheduled 15:30 sweep already
    # captured one for most legs; only the legs it could not price fall
    # through to the end-of-day hierarchy below.
    original_legs: dict[tuple[int, str], dict[int, dict]] = {}
    for key in legs_by_candidate:
        prior = (
            db.query(V4ShadowCandidateObservation)
            .filter_by(shadow_decision_id=key[0], candidate_id=key[1], phase="EXIT")
            .one_or_none()
        )
        rows_json = (prior.legs_json or {}).get("legs", []) if prior is not None else []
        original_legs[key] = {
            int(r["leg_index"]): r for r in rows_json if r.get("leg_index") is not None
        }

    # One market-data request and one closing-mark lookup per UNIQUE
    # contract, mapped back to every configuration that shares it.
    unique_conids: dict[str, tuple[int, str]] = {}
    for key, legs in legs_by_candidate.items():
        for leg in legs:
            if leg.conid:
                unique_conids.setdefault(leg.conid, key)
    summary.unique_contracts = len(unique_conids)

    quotes: dict[str, object] = {}
    closes: dict[str, Decimal | None] = {}
    close_sources: dict[str, str | None] = {}
    for conid, key in unique_conids.items():
        candidate = candidates[key]
        decision = decisions[key[0]]
        try:
            fetched = provider.get_quotes_for_known_contracts(
                decision.ticker,
                [
                    _known_contract(leg)
                    for leg in legs_by_candidate[key]
                    if leg.conid == conid
                ],
                candidate.expiration,
                now,
            )
            summary.quote_calls += 1
            if fetched:
                quotes[conid] = fetched[0]
        except Exception as exc:  # noqa: BLE001 -- one contract must not stop the rest
            summary.notes.append(f"quote failed for conId {conid}: {type(exc).__name__}: {exc}")
        try:
            close_value, close_source = provider.get_session_close_with_source(
                int(conid), session_date
            )
            closes[conid] = close_value
            close_sources[conid] = close_source
            summary.close_lookups += 1
        except Exception as exc:  # noqa: BLE001
            closes[conid] = None
            summary.notes.append(f"close failed for conId {conid}: {type(exc).__name__}: {exc}")

    underlying_closes: dict[str, Decimal | None] = {}
    for key in legs_by_candidate:
        ticker = decisions[key[0]].ticker
        if ticker in underlying_closes:
            continue
        expires_today = candidates[key].expiration == session_date
        if not expires_today:
            continue
        try:
            underlying_closes[ticker] = provider.get_underlying_session_close(
                ticker, session_date
            )
        except Exception as exc:  # noqa: BLE001
            underlying_closes[ticker] = None
            summary.notes.append(
                f"underlying close failed for {ticker}: {type(exc).__name__}: {exc}"
            )

    # Resolve every leg, then persist one recovery observation per candidate.
    observations: dict[tuple[int, str], V4ShadowCandidateObservation | None] = {}
    net_by_candidate: dict[tuple[int, str], Decimal | None] = {}
    method_by_candidate: dict[tuple[int, str], str | None] = {}
    for key, legs in legs_by_candidate.items():
        candidate = candidates[key]
        ticker = decisions[key[0]].ticker
        expires_today = candidate.expiration == session_date
        net = Decimal(0)
        rows: list[dict] = []
        unresolved: list[int] = []
        sources: set[str] = set()
        qualities: set[str] = set()
        for leg in legs:
            quote = quotes.get(leg.conid) if leg.conid else None
            side = "bid" if leg.action == "buy" else "ask"
            prior_row = original_legs.get(key, {}).get(leg.leg_index, {})
            prior_price = prior_row.get("price")
            captured = None if prior_price is None else Decimal(str(prior_price))
            fresh = getattr(quote, side, None) if quote is not None else None
            executable = captured if captured is not None else fresh
            executable_at = (
                "scheduled_window_capture" if captured is not None else "recovery_requote"
            )
            quality = (
                prior_row.get("market_data_quality")
                if captured is not None
                else (getattr(quote, "market_data_quality", None) if quote is not None else None)
            )
            resolution = resolve_leg_exit_price(
                action=leg.action,
                right=leg.right,
                strike=leg.strike,
                executable_price=executable,
                session_close=closes.get(leg.conid or ""),
                underlying_close=underlying_closes.get(ticker),
                expires_on_settlement_date=expires_today,
                book_empty=(
                    getattr(quote, f"{side}_book_empty", None) if quote is not None else None
                ),
                market_data_quality=quality,
            )
            if resolution.is_executable:
                resolution.provenance["executable_source"] = executable_at
                resolution.provenance["executable_observed_at"] = (
                    prior_row.get("retrieved_at") if captured is not None else now.isoformat()
                )
            leg.resolution = resolution
            if quality:
                qualities.add(quality)
            if resolution.resolved and resolution.price is not None:
                sign = Decimal(1) if leg.action == "buy" else Decimal(-1)
                net += sign * resolution.price * Decimal(leg.quantity) * leg.multiplier
                if resolution.pricing_source:
                    sources.add(resolution.pricing_source)
            else:
                unresolved.append(leg.leg_index)
            rows.append(
                {
                    "leg_index": leg.leg_index,
                    "action": leg.action,
                    "right": leg.right,
                    "strike": str(leg.strike),
                    "external_contract_id": leg.conid,
                    "required_side": resolution.required_side,
                    "price": None if resolution.price is None else str(resolution.price),
                    "pricing_source": resolution.pricing_source,
                    "unresolved_reason": resolution.unresolved_reason,
                    "bid": prior_row.get("bid") or _s(getattr(quote, "bid", None)),
                    "ask": prior_row.get("ask") or _s(getattr(quote, "ask", None)),
                    "last": prior_row.get("last") or _s(getattr(quote, "last_price", None)),
                    "requote_bid": _s(getattr(quote, "bid", None)),
                    "requote_ask": _s(getattr(quote, "ask", None)),
                    "original_required_side_state": prior_row.get("required_side_state"),
                    "option_session_close": _s(closes.get(leg.conid or "")),
                    "option_session_close_source": close_sources.get(leg.conid or ""),
                    "underlying_session_close": _s(underlying_closes.get(ticker)),
                    "market_data_quality": quality,
                    "provenance": resolution.provenance,
                    "observed_at": now.isoformat(),
                }
            )
        net_by_candidate[key] = None if unresolved else net
        method = "+".join(sorted(sources)) if sources else None
        method_by_candidate[key] = method
        obs = None
        if not dry_run:
            existing = (
                db.query(V4ShadowCandidateObservation)
                .filter_by(
                    shadow_decision_id=key[0], candidate_id=key[1], phase=RECOVERY_PHASE
                )
                .one_or_none()
            )
            obs = existing or V4ShadowCandidateObservation(
                shadow_decision_id=key[0],
                candidate_id=key[1],
                phase=RECOVERY_PHASE,
                observed_at=now,
                status="NOT_EXECUTABLE" if unresolved else "OBSERVED",
                failure_category=RECOVERY_UNRESOLVED if unresolved else None,
                failure_detail=(
                    f"required exit side unavailable and no permitted end-of-day fallback "
                    f"applied on leg(s) {unresolved}"
                    if unresolved
                    else None
                ),
                net_executable_value=None if unresolved else net,
                market_data_quality=(
                    next(iter(qualities))
                    if len(qualities) == 1
                    else ("mixed:" + ",".join(sorted(qualities)) if qualities else None)
                ),
                source_provider="ibkr_tws",
                unique_contract_count=len({leg.conid for leg in legs if leg.conid}) or None,
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
        summary.candidate_rows.append(
            {
                "ticker": ticker,
                "candidate_id": key[1],
                "expiration": candidate.expiration.isoformat(),
                "expires_on_settlement_date": expires_today,
                "unresolved_legs": unresolved,
                "pricing_method": method,
                "net_exit_per_unit": None if unresolved else str(net),
                "legs": rows,
            }
        )
    if not dry_run:
        db.flush()

    # One NEW settlement row per configuration, for its own frozen quantity.
    for row in pending:
        key = (row.shadow_decision_id, row.candidate_id)
        entry = (
            db.query(V4ShadowConfigEntry)
            .filter_by(shadow_config_result_id=row.shadow_config_result_id)
            .one_or_none()
        )
        if entry is None:
            summary.notes.append(f"no frozen entry for config result {row.shadow_config_result_id}")
            continue
        net_exit: Decimal | None = net_by_candidate.get(key)
        obs = observations.get(key)
        result_row = {
            "ticker": decisions[key[0]].ticker,
            "configuration_key": row.configuration_key,
            "candidate_id": row.candidate_id,
            "original_status": row.status,
            "original_failure_category": row.failure_category,
            "original_settlement_id": row.id,
            "original_settled_at": row.settled_at.isoformat(),
            "quantity": entry.quantity,
            "entry_net_value": str(entry.entry_net_value or Decimal(0)),
        }
        if net_exit is None:
            result_row.update(
                {"final_status": "OBSERVATION_FAILED", "failure": RECOVERY_UNRESOLVED}
            )
            summary.unresolved += 1
            if not dry_run:
                db.add(
                    V4ShadowConfigSettlement(
                        timing_policy_version=row.timing_policy_version,
                        shadow_config_result_id=row.shadow_config_result_id,
                        shadow_decision_id=row.shadow_decision_id,
                        candidate_observation_id=obs.id if obs else None,
                        configuration_key=row.configuration_key,
                        candidate_id=row.candidate_id,
                        status="OBSERVATION_FAILED",
                        quantity=entry.quantity,
                        standardized_capital=entry.standardized_capital,
                        capital_used=entry.capital_used,
                        entry_net_value=entry.entry_net_value,
                        entry_observed_at=entry.observed_at,
                        settled_at=now,
                        pricing_convention=RECOVERY_PROVENANCE,
                        pricing_method=None,
                        recovery_provenance=RECOVERY_PROVENANCE,
                        supersedes_settlement_id=row.id,
                        market_data_quality=obs.market_data_quality if obs else None,
                        failure_category=RECOVERY_UNRESOLVED,
                        failure_detail=obs.failure_detail if obs else RECOVERY_UNRESOLVED,
                    )
                )
        else:
            exit_value = net_exit * entry.quantity
            entry_value = entry.entry_net_value or Decimal(0)
            realized = exit_value - entry_value
            method = method_by_candidate.get(key)
            result_row.update(
                {
                    "final_status": "SETTLED",
                    "exit_net_value": str(exit_value),
                    "realized_pnl": str(realized),
                    "return_on_standardized_capital": str(
                        realized / entry.standardized_capital
                    ),
                    "pricing_method": method,
                }
            )
            summary.settled += 1
            if not dry_run:
                db.add(
                    V4ShadowConfigSettlement(
                        timing_policy_version=row.timing_policy_version,
                        shadow_config_result_id=row.shadow_config_result_id,
                        shadow_decision_id=row.shadow_decision_id,
                        candidate_observation_id=obs.id if obs else None,
                        configuration_key=row.configuration_key,
                        candidate_id=row.candidate_id,
                        status="SETTLED",
                        quantity=entry.quantity,
                        standardized_capital=entry.standardized_capital,
                        capital_used=entry.capital_used,
                        entry_net_value=entry_value,
                        exit_net_value=exit_value,
                        realized_pnl=realized,
                        return_on_standardized_capital=realized / entry.standardized_capital,
                        entry_observed_at=entry.observed_at,
                        settled_at=now,
                        pricing_convention=_convention(method),
                        pricing_method=method,
                        recovery_provenance=RECOVERY_PROVENANCE,
                        supersedes_settlement_id=row.id,
                        market_data_quality=obs.market_data_quality if obs else None,
                    )
                )
        summary.config_rows.append(result_row)
    if not dry_run:
        db.commit()
    return summary


def _convention(method: str | None) -> str:
    """A compact aggregate that fits the column; the explicit per-leg
    pricing-source labels are persisted in the observation's legs_json."""
    if not method:
        return RECOVERY_PROVENANCE
    if any(source in method for source in FALLBACK_PRICING_SOURCES):
        return "V4_EXIT_EOD_FALLBACK"
    return "V4_EXIT_EXECUTABLE"


def _s(value) -> str | None:
    return None if value is None else str(value)


def _known_contract(leg: RecoveryLeg):
    from providers.types import KnownContract  # noqa: PLC0415

    return KnownContract(
        external_contract_id=str(leg.conid),
        strike=leg.strike,
        option_type=leg.right,
        action=leg.action,
    )


__all__ = [
    "FALLBACK_PRICING_SOURCES",
    "QuoteRequirement",
    "RECOVERY_PHASE",
    "RECOVERY_PROVENANCE",
    "RECOVERY_UNRESOLVED",
    "RecoverySummary",
    "recover_due_settlements",
]
