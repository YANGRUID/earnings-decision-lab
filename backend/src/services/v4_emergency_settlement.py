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
from services.v4_settlement_sessions import (
    SettlementSessionResolver,
    eastern_date,
    session_has_closed,
)

RECOVERY_PHASE = "EXIT_EOD"
#: A recovery that runs on a LATER session than the one it settles needs its
#: own evidence row. The unique index on (decision, candidate, phase) allows
#: exactly one observation per phase, and a session whose own 16:30 attempt
#: already failed has used EXIT_EOD up -- reusing that row silently attached
#: recovered settlements to an observation that said NOT_EXECUTABLE and
#: carried no closing marks at all (found on the JNJ rows, 2026-10-16).
LATE_RECOVERY_PHASE = "EXIT_LATE_RECOVERY"
RECOVERY_PROVENANCE = "LATE_SETTLEMENT_OVERRIDE"
RECOVERY_UNRESOLVED = "MARKET_DATA_UNAVAILABLE_AFTER_EMERGENCY_RETRY"
#: Why a leg took no executable price on a cross-session recovery: a quote
#: taken now is NOW's price, and for a position due in an earlier session
#: that is not a late fill, it is a different trade.
REQUOTE_SUPPRESSED_CROSS_SESSION = "requote_suppressed_cross_session"


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
    #: Configurations left alone because both recovery evidence phases are
    #: already used -- see _free_observation_phase.
    skipped_exhausted: int = 0
    candidate_rows: list[dict] = field(default_factory=list)
    config_rows: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _free_observation_phase(db: Session, decision_id: int, candidate_id: str) -> str | None:
    """A free observation-phase slot for THIS recovery's own evidence.

    (decision, candidate, phase) is unique, so a recovery cannot share a
    phase with an earlier attempt -- and must not, because that earlier row
    is the immutable record of what that attempt saw. EXIT_EOD is used by
    the first recovery of a candidate and EXIT_LATE_RECOVERY by a second;
    beyond that there is no slot, and the caller leaves the position open
    rather than settling it with nowhere to put the provenance.
    """
    taken = {
        row.phase
        for row in db.query(V4ShadowCandidateObservation)
        .filter_by(shadow_decision_id=decision_id, candidate_id=candidate_id)
        .all()
    }
    for phase in (RECOVERY_PHASE, LATE_RECOVERY_PHASE):
        if phase not in taken:
            return phase
    return None


def _unsettled_attempts(db: Session) -> list[V4ShadowConfigSettlement]:
    """The earliest still-open failed attempt per configuration, oldest
    first. A configuration that already has ANY settled row is never
    returned -- that is the idempotency guarantee."""
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
        seen.add(row.shadow_config_result_id)
        out.append(row)
    return out


def _pending_configs(
    db: Session, session_date: date, resolver: SettlementSessionResolver
) -> list[V4ShadowConfigSettlement]:
    """The still-unsettled configurations whose LEGAL settlement session is
    ``session_date``.

    The session comes from the calendar event via the active timing policy,
    never from when the failed attempt happened to be stamped: see
    services/v4_settlement_sessions.py for the NKE case where those differ
    by three sessions and the stamp-based match could never recover.
    """
    out: list[V4ShadowConfigSettlement] = []
    for row in _unsettled_attempts(db):
        decision = db.get(V4ShadowDecision, row.shadow_decision_id)
        if decision is None:
            continue
        if resolver.for_event(decision.earnings_calendar_event_id) != session_date:
            continue
        out.append(row)
    return out


def pending_settlement_sessions(db: Session) -> list[date]:
    """Every legal settlement session that still has an unsettled control
    configuration, oldest first -- what a catch-up sweep iterates."""
    resolver = SettlementSessionResolver(db)
    sessions: set[date] = set()
    for row in _unsettled_attempts(db):
        decision = db.get(V4ShadowDecision, row.shadow_decision_id)
        if decision is None:
            continue
        resolved = resolver.for_event(decision.earnings_calendar_event_id)
        if resolved is not None:
            sessions.add(resolved)
    return sorted(sessions)


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
    # Whether a FINAL same-session closing mark can exist yet. Before 16:00 ET
    # on the settlement date it cannot: the last intraday bar is a real
    # same-session price but it is not a close, and labelling it
    # MARKET_CLOSE_FALLBACK would claim a finality the session has not
    # reached. Executable prices are unaffected -- those are still preferred
    # and still settle mid-session.
    session_close_is_final = session_has_closed(now, session_date)
    if not session_close_is_final:
        summary.notes.append(
            f"session {session_date.isoformat()} has not closed at {now.isoformat()}: "
            "executable sides are settled now, and any leg with an empty book is left "
            "unresolved for a post-close retry rather than priced from an intraday bar"
        )
    # Is this recovery running on the session it is settling, or later?
    same_session = eastern_date(now) == session_date
    if not same_session:
        summary.notes.append(
            f"cross-session recovery: settling session {session_date.isoformat()} from "
            f"{eastern_date(now).isoformat()}, so no leg is requoted -- the only "
            "executable prices admitted are the ones the original legal window captured"
        )

    resolver = SettlementSessionResolver(db)
    pending = _pending_configs(db, session_date, resolver)
    if not pending:
        summary.notes.append("no unsettled configurations due on this session date")
        return summary

    # A candidate with no free evidence phase has already had its two
    # recovery attempts; it is dropped from the work set entirely rather
    # than re-priced. That is what makes a catch-up sweep idempotent: no
    # further market-data calls, and no third failed settlement row piling
    # up on a position that is simply not recoverable.
    phases: dict[tuple[int, str], str] = {}
    exhausted: list[V4ShadowConfigSettlement] = []
    for row in pending:
        key = (row.shadow_decision_id, row.candidate_id)
        if key not in phases:
            free = _free_observation_phase(db, key[0], key[1])
            if free is None:
                exhausted.append(row)
                continue
            phases[key] = free
    if exhausted:
        pending = [row for row in pending if row not in exhausted]
        summary.skipped_exhausted = len(exhausted)
        summary.notes.append(
            f"{len(exhausted)} configuration(s) skipped: both recovery evidence phases "
            "are already used, so a further attempt could not persist its own "
            "closing-mark provenance"
        )
    if not pending:
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
        if same_session:
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
                summary.notes.append(
                    f"quote failed for conId {conid}: {type(exc).__name__}: {exc}"
                )
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
                session_close_is_final=session_close_is_final,
            )
            if resolution.is_executable:
                resolution.provenance["executable_source"] = executable_at
                resolution.provenance["executable_observed_at"] = (
                    prior_row.get("retrieved_at") if captured is not None else now.isoformat()
                )
            if not same_session:
                resolution.provenance["requote"] = REQUOTE_SUPPRESSED_CROSS_SESSION
            resolution.provenance["settlement_session"] = session_date.isoformat()
            resolution.provenance["closing_mark_source"] = close_sources.get(leg.conid or "")
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
        obs_phase = phases[key]
        if not dry_run:
            obs = V4ShadowCandidateObservation(
                shadow_decision_id=key[0],
                candidate_id=key[1],
                phase=obs_phase,
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
                    "settlement_session_source": "calendar_event+active_timing_policy",
                    "recovered_on_session": eastern_date(now).isoformat(),
                    "requote": None if same_session else REQUOTE_SUPPRESSED_CROSS_SESSION,
                },
            )
            db.add(obs)
        observations[key] = obs
        summary.candidate_rows.append(
            {
                "ticker": ticker,
                "candidate_id": key[1],
                "expiration": candidate.expiration.isoformat(),
                "expires_on_settlement_date": expires_today,
                "settlement_session": session_date.isoformat(),
                "evidence_phase": obs_phase,
                "unresolved_legs": unresolved,
                "pricing_method": method,
                "net_exit_per_unit": None if net_by_candidate[key] is None else str(net),
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
    "LATE_RECOVERY_PHASE",
    "QuoteRequirement",
    "RECOVERY_PHASE",
    "RECOVERY_PROVENANCE",
    "RECOVERY_UNRESOLVED",
    "REQUOTE_SUPPRESSED_CROSS_SESSION",
    "RecoverySummary",
    "pending_settlement_sessions",
    "recover_due_settlements",
]
