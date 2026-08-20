"""Phase 4.3 -- read-only endpoints over the immutable decision_snapshot
table. No mutation endpoint exists, or ever will for this table --
freezing only ever happens from services/decision_pipeline.py, never
from an HTTP request, the same standing decision already made for
entry_snapshot/settlement_snapshot: not exposing a write path is a
stronger guarantee than exposing one and trusting callers not to misuse
it.
"""

from fastapi import APIRouter, Query

from api.deps import DbSession
from api.exceptions import NotFoundError
from models.decision_snapshot import DecisionSnapshot
from schemas.api import DecisionSnapshotResponse

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
    return (
        query.order_by(DecisionSnapshot.generated_at.desc()).offset(offset).limit(limit).all()
    )


@router.get("/{snapshot_id}", response_model=DecisionSnapshotResponse)
def get_decision_snapshot(snapshot_id: int, db: DbSession) -> DecisionSnapshot:
    snapshot = db.get(DecisionSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError(f"decision snapshot {snapshot_id} not found")
    return snapshot
