"""Phase 4.4 -- read-only, system-wide view over every official
benchmark entry capture attempt. No mutation endpoint -- see
api/routers/decision_snapshots.py's own docstring for why.
"""

from fastapi import APIRouter, Query

from api.deps import DbSession
from models.entry_capture_attempt import EntryCaptureAttempt
from schemas.api import EntryCaptureAttemptResponse

router = APIRouter(prefix="/benchmark", tags=["benchmark"])


@router.get("/entries", response_model=list[EntryCaptureAttemptResponse])
def list_benchmark_entries(
    db: DbSession,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[EntryCaptureAttempt]:
    query = db.query(EntryCaptureAttempt)
    if status:
        query = query.filter(EntryCaptureAttempt.status == status.upper())
    return (
        query.order_by(EntryCaptureAttempt.id.desc()).offset(offset).limit(limit).all()
    )
