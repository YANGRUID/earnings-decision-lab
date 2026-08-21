"""Phase 4.5 -- read-only endpoint over the immutable settlement_
capture_attempt table. No mutation endpoint exists, or ever will --
settlement only ever happens from services/benchmark_exit_capture.py,
scheduled by services/scheduler.py::run_exit_capture_job, never from an
HTTP request -- the same standing decision already made for entry_
capture_attempt/entry_snapshot (see api/routers/decision_snapshots.py's
own docstring): not exposing a write path is a stronger guarantee than
exposing one and trusting callers not to misuse it.
"""

from fastapi import APIRouter

from api.deps import DbSession
from api.exceptions import NotFoundError
from models.decision_snapshot import DecisionSnapshot
from models.settlement_capture_attempt import SettlementCaptureAttempt
from schemas.api import SettlementCaptureAttemptResponse

router = APIRouter(prefix="/settlements", tags=["settlements"])


@router.get("/{decision_id}", response_model=list[SettlementCaptureAttemptResponse])
def list_settlements(decision_id: int, db: DbSession) -> list[SettlementCaptureAttempt]:
    """Every settlement (exit) capture attempt on record for this
    decision -- successful and failed alike, oldest first, so a reader
    can see the real retry history rather than only the operative
    outcome (mirrors GET /decision-snapshots/{id}/entries exactly)."""
    snapshot = db.get(DecisionSnapshot, decision_id)
    if snapshot is None:
        raise NotFoundError(f"decision snapshot {decision_id} not found")
    return (
        db.query(SettlementCaptureAttempt)
        .filter(SettlementCaptureAttempt.decision_snapshot_id == decision_id)
        .order_by(SettlementCaptureAttempt.id.asc())
        .all()
    )
