"""Persists real, per-poll quote-acquisition telemetry (IBKR execution-
observability hardening, 2026-08-26) for FUTURE official entry/settlement
capture attempts -- see models/quote_acquisition_attempt.py's own
docstring for why this is a separate, append-only diagnostic table,
never a widening of the official trade-evidence tables themselves.

Two thin, capture-type-specific entry points (``persist_entry_quote_
telemetry`` / ``persist_settlement_quote_telemetry``) share one real
persistence routine -- the two capture flows differ only in how a
conid's leg_index/required_side get derived (decision_snapshot.legs +
entry_requirement_for_action for entry; EntrySnapshot rows +
exit_requirement_for_action for settlement, the exact inverse mapping),
never in how a telemetry row itself is built.

Never influences official pricing or capture success/failure -- both
entry points are called strictly AFTER the real provider call already
returned (or raised, in which case the caller never reaches this module
at all -- see Section 9's own "instrumentation must not influence
trading behavior" rule), purely to record what was observed. The
official fill still comes only from EntrySnapshot.benchmark_entry_price /
ExitSnapshot.benchmark_exit_price, computed exactly as before.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from models.enums import OptionType, QuoteAcquisitionCaptureType, QuoteRequirement
from models.quote_acquisition_attempt import QuoteAcquisitionAttempt
from providers.types import (
    OptionQuote,
    SnapshotAttempt,
    entry_requirement_for_action,
    exit_requirement_for_action,
)
from services.entry_failure_taxonomy import ProviderExceptionClassification

if TYPE_CHECKING:
    from models.entry_snapshot import EntrySnapshot

_OPTION_TYPE_BY_STR = {"call": OptionType.CALL, "put": OptionType.PUT}


@dataclass(frozen=True)
class _LegContext:
    strike: Decimal
    option_type: OptionType
    leg_index: int | None
    required_side: QuoteRequirement


def _persist(
    db: Session,
    *,
    capture_attempt_type: QuoteAcquisitionCaptureType,
    entry_capture_attempt_id: int | None,
    settlement_capture_attempt_id: int | None,
    ticker: str,
    expiration: date,
    attempts: list[SnapshotAttempt],
    context_by_conid: dict[int, _LegContext],
    unresolved: list[_LegContext],
    classification: ProviderExceptionClassification | None = None,
) -> None:
    """``unresolved`` -- legs whose exact contract could not be resolved
    at all, persisted as a single, real ``snapshot_attempt_number=0`` row
    each: a resolution failure is itself diagnostically valuable, distinct
    from "resolved but no bid/ask ever arrived" (which shows up as a
    real, still-incomplete poll row instead).

    ``classification`` (Section 10) -- set only by the exception-path
    callers below (persist_entry_exception_telemetry / persist_
    settlement_exception_telemetry), when a real provider exception
    aborted the capture before any quote resolved at all, so every
    ``unresolved`` row here carries the real rate_limited/permission_
    error/provider_error_category/contract_resolved signal from that
    exception instead of the plain "no contract could be resolved,
    reason unknown" defaults a normal unresolved-contract row gets.
    """
    now = datetime.now(UTC)
    for leg in unresolved:
        db.add(
            QuoteAcquisitionAttempt(
                capture_attempt_type=capture_attempt_type,
                entry_capture_attempt_id=entry_capture_attempt_id,
                settlement_capture_attempt_id=settlement_capture_attempt_id,
                ticker=ticker,
                leg_index=leg.leg_index,
                expiration=expiration,
                option_type=leg.option_type,
                strike=leg.strike,
                external_contract_id=None,
                required_side=leg.required_side,
                snapshot_attempt_number=0,
                observed_at=now,
                elapsed_ms=0,
                bid_present=False,
                ask_present=False,
                last_present=False,
                market_data_quality=None,
                rate_limited=classification.rate_limited if classification else False,
                permission_error=classification.permission_error if classification else False,
                provider_error_category=classification.category if classification else None,
                contract_resolved=classification.contract_resolved if classification else False,
                final_for_leg=True,
            )
        )

    last_attempt_number = attempts[-1].attempt if attempts else None
    for attempt in attempts:
        for conid, presence in attempt.per_conid.items():
            known_leg = context_by_conid.get(conid)
            db.add(
                QuoteAcquisitionAttempt(
                    capture_attempt_type=capture_attempt_type,
                    entry_capture_attempt_id=entry_capture_attempt_id,
                    settlement_capture_attempt_id=settlement_capture_attempt_id,
                    ticker=ticker,
                    leg_index=known_leg.leg_index if known_leg else None,
                    expiration=expiration,
                    option_type=known_leg.option_type if known_leg else None,
                    strike=known_leg.strike if known_leg else None,
                    external_contract_id=str(conid),
                    required_side=(
                        known_leg.required_side if known_leg else QuoteRequirement.ANALYTICAL
                    ),
                    snapshot_attempt_number=attempt.attempt,
                    observed_at=now,
                    elapsed_ms=int(round(attempt.elapsed_ms)),
                    bid_present=presence.bid_present,
                    ask_present=presence.ask_present,
                    last_present=presence.last_present,
                    bid=presence.bid,
                    ask=presence.ask,
                    last_price=presence.last_price,
                    market_data_quality=presence.market_data_quality,
                    rate_limited=False,
                    permission_error=False,
                    contract_resolved=True,
                    final_for_leg=attempt.attempt == last_attempt_number,
                )
            )
    db.flush()


def persist_entry_quote_telemetry(
    db: Session,
    *,
    entry_capture_attempt_id: int,
    ticker: str,
    expiration: date,
    decision_legs: list[dict],
    attempts: list[SnapshotAttempt],
    quotes: list[OptionQuote],
) -> None:
    """``decision_legs`` is ``decision_snapshot.legs`` -- the real,
    already-selected strike/option_type/action for each leg (see
    services/decision_engine.py::leg_to_dict). ``quotes`` is what
    ``get_quotes_for_selected_legs`` returned; used only to recover
    which real conid corresponds to which leg (a resolved contract's
    strike/option_type is on the quote itself, not on ``attempts``)."""
    resolved_keys = {(q.strike, q.option_type) for q in quotes if q.external_contract_id}
    context_by_conid: dict[int, _LegContext] = {}
    for q in quotes:
        if q.external_contract_id is None:
            continue
        leg_index = next(
            (
                i
                for i, leg in enumerate(decision_legs)
                if Decimal(leg["strike"]) == q.strike and leg["option_type"] == q.option_type
            ),
            None,
        )
        action = decision_legs[leg_index].get("action") if leg_index is not None else None
        context_by_conid[int(q.external_contract_id)] = _LegContext(
            strike=q.strike,
            option_type=_OPTION_TYPE_BY_STR[q.option_type],
            leg_index=leg_index,
            required_side=entry_requirement_for_action(action),
        )

    unresolved = [
        _LegContext(
            strike=Decimal(leg["strike"]),
            option_type=_OPTION_TYPE_BY_STR[leg["option_type"]],
            leg_index=i,
            required_side=entry_requirement_for_action(leg.get("action")),
        )
        for i, leg in enumerate(decision_legs)
        if (Decimal(leg["strike"]), leg["option_type"]) not in resolved_keys
    ]

    _persist(
        db,
        capture_attempt_type=QuoteAcquisitionCaptureType.ENTRY,
        entry_capture_attempt_id=entry_capture_attempt_id,
        settlement_capture_attempt_id=None,
        ticker=ticker,
        expiration=expiration,
        attempts=attempts,
        context_by_conid=context_by_conid,
        unresolved=unresolved,
    )


def persist_settlement_quote_telemetry(
    db: Session,
    *,
    settlement_capture_attempt_id: int,
    ticker: str,
    expiration: date,
    entry_legs: list["EntrySnapshot"],
    attempts: list[SnapshotAttempt],
    quotes: list[OptionQuote],
) -> None:
    """``entry_legs`` are the real EntrySnapshot rows being closed --
    already-known conid/strike/option_type/action, no re-resolution."""
    resolved_conids = {
        int(q.external_contract_id) for q in quotes if q.external_contract_id is not None
    }
    context_by_conid: dict[int, _LegContext] = {}
    unresolved: list[_LegContext] = []
    for entry_leg in entry_legs:
        if entry_leg.external_contract_id is None or entry_leg.strike is None:
            continue
        try:
            conid = int(entry_leg.external_contract_id)
        except ValueError:
            continue
        required = exit_requirement_for_action(
            entry_leg.action.value if entry_leg.action is not None else None
        )
        leg_ctx = _LegContext(
            strike=entry_leg.strike,
            option_type=entry_leg.option_type,  # type: ignore[arg-type]
            leg_index=entry_leg.leg_index,
            required_side=required,
        )
        if conid in resolved_conids:
            context_by_conid[conid] = leg_ctx
        else:
            unresolved.append(leg_ctx)

    _persist(
        db,
        capture_attempt_type=QuoteAcquisitionCaptureType.SETTLEMENT,
        entry_capture_attempt_id=None,
        settlement_capture_attempt_id=settlement_capture_attempt_id,
        ticker=ticker,
        expiration=expiration,
        attempts=attempts,
        context_by_conid=context_by_conid,
        unresolved=unresolved,
    )


def persist_entry_exception_telemetry(
    db: Session,
    *,
    entry_capture_attempt_id: int,
    ticker: str,
    expiration: date,
    decision_legs: list[dict],
    classification: ProviderExceptionClassification,
) -> None:
    """Section 10 -- a real provider exception (rate limit, permission
    error, gateway unreachable/timeout, contract-resolution failure)
    aborted this capture before ``get_quotes_for_selected_legs`` (or the
    underlying-quote call alongside it) ever returned, so there is no
    resolved ``quotes`` list to match a conid back to a leg by (the
    normal persist_entry_quote_telemetry path). Every requested leg still
    gets one honest, structured diagnostic row -- matched directly against
    ``decision_legs`` (no quote needed for strike/option_type/action),
    carrying the real exception classification, so a future failure like
    this is diagnosable from structured evidence, not only EntryCapture
    Attempt.capture_error free text. Never persists a bid/ask/last value
    (none was ever observed) and never influences the FAILED outcome
    already decided by the caller before this runs.
    """
    unresolved = [
        _LegContext(
            strike=Decimal(leg["strike"]),
            option_type=_OPTION_TYPE_BY_STR[leg["option_type"]],
            leg_index=i,
            required_side=entry_requirement_for_action(leg.get("action")),
        )
        for i, leg in enumerate(decision_legs)
    ]
    _persist(
        db,
        capture_attempt_type=QuoteAcquisitionCaptureType.ENTRY,
        entry_capture_attempt_id=entry_capture_attempt_id,
        settlement_capture_attempt_id=None,
        ticker=ticker,
        expiration=expiration,
        attempts=[],
        context_by_conid={},
        unresolved=unresolved,
        classification=classification,
    )


def persist_settlement_exception_telemetry(
    db: Session,
    *,
    settlement_capture_attempt_id: int,
    ticker: str,
    expiration: date,
    entry_legs: list["EntrySnapshot"],
    classification: ProviderExceptionClassification,
) -> None:
    """Settlement's mirror of persist_entry_exception_telemetry -- matched
    directly against the real, already-known EntrySnapshot rows being
    closed (strike/option_type/action, exactly as persist_settlement_
    quote_telemetry's own normal path uses), since there is no resolved
    ``quotes`` list once the provider call itself raised."""
    unresolved = [
        _LegContext(
            strike=entry_leg.strike,
            option_type=entry_leg.option_type,  # type: ignore[arg-type]
            leg_index=entry_leg.leg_index,
            required_side=exit_requirement_for_action(
                entry_leg.action.value if entry_leg.action is not None else None
            ),
        )
        for entry_leg in entry_legs
        if entry_leg.strike is not None
    ]
    _persist(
        db,
        capture_attempt_type=QuoteAcquisitionCaptureType.SETTLEMENT,
        entry_capture_attempt_id=None,
        settlement_capture_attempt_id=settlement_capture_attempt_id,
        ticker=ticker,
        expiration=expiration,
        attempts=[],
        context_by_conid={},
        unresolved=unresolved,
        classification=classification,
    )
