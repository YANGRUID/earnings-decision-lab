"""Phase 4.3 sec 0A's resolution, made concrete: DecisionSnapshot stays
immutable forever -- no ``status`` column, no UPDATEs, ever. A
decision's real lifecycle (pending / entered / settled) is derived by
querying its immutable child records, never by mutating the snapshot
itself:

    DecisionSnapshot
        |
        +-- EntryCaptureAttempt with status=CAPTURED exists?
                |
                +-- yes -> ENTERED
                |
                +-- no  -> PENDING_ENTRY (still due, or every attempt so
                            far failed -- see EntryCaptureAttempt rows
                            directly for the honest reason why)
                |
                +-- (Phase 4.5) SettlementCaptureAttempt with
                    status=CAPTURED exists too? -> SETTLED

A genuinely mutable "workflow/execution state" table was considered for
this (Phase 4.3's own report raised it as a possibility) and rejected as
unnecessary: every fact this module needs is already a real, queryable,
append-only row (EntryCaptureAttempt, SettlementCaptureAttempt) -- there
is no information here that isn't already durably recorded somewhere
immutable. This module is pure read-side derivation, nothing else; it
never writes.

Phase 4.5 note: SETTLED is derived from ``settlement_capture_attempt``
(new table, mirrors entry_capture_attempt exactly -- see
models/settlement_capture_attempt.py), not the older ``settlement_
snapshot`` (a Phase 4.1 scaffold, since superseded and never written to
-- see PHASE4_5_SETTLEMENT_ARCHITECTURE_REVIEW.md's 2026-08-21
addendum). ``settlement_snapshot`` is deliberately left untouched.
"""

from enum import StrEnum

from sqlalchemy.orm import Session

from models.entry_capture_attempt import EntryCaptureAttempt
from models.enums import CaptureStatus
from models.settlement_capture_attempt import SettlementCaptureAttempt


class DecisionLifecycleStage(StrEnum):
    PENDING_ENTRY = "pending_entry"
    ENTERED = "entered"
    SETTLED = "settled"


def has_official_entry(db: Session, decision_snapshot_id: int, portfolio_id: int) -> bool:
    """True only when a complete, coherent entry capture exists -- a
    partial or failed EntryCaptureAttempt does not count (Phase 4.4 sec
    13: no partial multi-leg entry counts as a benchmark trade)."""
    return (
        db.query(EntryCaptureAttempt)
        .filter_by(
            decision_snapshot_id=decision_snapshot_id,
            benchmark_portfolio_id=portfolio_id,
            status=CaptureStatus.CAPTURED,
        )
        .first()
        is not None
    )


def has_settlement(db: Session, decision_snapshot_id: int, portfolio_id: int) -> bool:
    """True only when a complete, coherent settlement (exit) capture
    exists -- mirrors has_official_entry's own all-or-nothing rule and
    portfolio scoping exactly (Phase 4.5: no partial multi-leg exit
    counts as a real, closed benchmark position)."""
    return (
        db.query(SettlementCaptureAttempt)
        .filter_by(
            decision_snapshot_id=decision_snapshot_id,
            benchmark_portfolio_id=portfolio_id,
            status=CaptureStatus.CAPTURED,
        )
        .first()
        is not None
    )


def decision_lifecycle_stage(
    db: Session, decision_snapshot_id: int, portfolio_id: int
) -> DecisionLifecycleStage:
    if has_settlement(db, decision_snapshot_id, portfolio_id):
        return DecisionLifecycleStage.SETTLED
    if has_official_entry(db, decision_snapshot_id, portfolio_id):
        return DecisionLifecycleStage.ENTERED
    return DecisionLifecycleStage.PENDING_ENTRY
