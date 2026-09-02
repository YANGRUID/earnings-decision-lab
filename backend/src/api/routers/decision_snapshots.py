"""Phase 4.3/4.4 -- read-only endpoints over the immutable
decision_snapshot table and its entry-capture attempts. No mutation
endpoint exists, or ever will for either -- freezing/capture only ever
happen from services/decision_pipeline.py and services/benchmark_entry_
capture.py, never from an HTTP request, the same standing decision
already made for entry_snapshot/settlement_snapshot: not exposing a
write path is a stronger guarantee than exposing one and trusting
callers not to misuse it (Phase 4.4 sec 16/17: no brokerage execution,
no manual trade endpoint, ever).
"""

from fastapi import APIRouter, Query

from api.deps import DbSession
from api.exceptions import NotFoundError
from models.decision_snapshot import DecisionSnapshot
from models.entry_capture_attempt import EntryCaptureAttempt
from schemas.api import (
    DecisionSnapshotResponse,
    EntryCaptureAttemptResponse,
    ForwardTestDatasetResponse,
)
from services.forward_test_dataset import MAX_DATASET_ROWS, list_forward_test_dataset

router = APIRouter(prefix="/decision-snapshots", tags=["decision-snapshots"])


@router.get("", response_model=list[DecisionSnapshotResponse])
def list_decision_snapshots(
    db: DbSession,
    ticker: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[DecisionSnapshot]:
    query = db.query(DecisionSnapshot)
    if ticker:
        query = query.filter(DecisionSnapshot.ticker == ticker.upper())
    if status:
        query = query.filter(DecisionSnapshot.status == status.upper())
    return query.order_by(DecisionSnapshot.generated_at.desc()).offset(offset).limit(limit).all()


@router.get("/forward-test-dataset", response_model=ForwardTestDatasetResponse)
def get_forward_test_dataset(
    db: DbSession,
    limit: int = Query(default=MAX_DATASET_ROWS, ge=1, le=MAX_DATASET_ROWS),
) -> ForwardTestDatasetResponse:
    """Phase 4 forward-test evaluation dataset (2026-08-26), Section
    32-33 -- a canonical, READ-ONLY view over the existing official
    evidence, built for future evaluation/modeling work. Registered
    before ``/{snapshot_id}`` so this static path is never shadowed by
    that dynamic one. Does NOT itself train, fit, or calibrate anything
    -- see services/forward_test_dataset.py's own module docstring and
    this phase's final report Section L (model training explicitly
    deferred -- insufficient real settled sample size today)."""
    return ForwardTestDatasetResponse(
        rows=list_forward_test_dataset(db, limit=limit)  # type: ignore[arg-type]
    )


@router.get("/{snapshot_id}", response_model=DecisionSnapshotResponse)
def get_decision_snapshot(snapshot_id: int, db: DbSession) -> DecisionSnapshot:
    snapshot = db.get(DecisionSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError(f"decision snapshot {snapshot_id} not found")
    return snapshot


@router.get("/{snapshot_id}/entries", response_model=list[EntryCaptureAttemptResponse])
def list_decision_snapshot_entries(snapshot_id: int, db: DbSession) -> list[EntryCaptureAttempt]:
    """Every entry capture attempt on record for this decision --
    successful and failed alike, oldest first, so a reader can see the
    real retry history rather than only the operative outcome."""
    snapshot = db.get(DecisionSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError(f"decision snapshot {snapshot_id} not found")
    return (
        db.query(EntryCaptureAttempt)
        .filter(EntryCaptureAttempt.decision_snapshot_id == snapshot_id)
        .order_by(EntryCaptureAttempt.id.asc())
        .all()
    )
