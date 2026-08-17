from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from api.deps import DbSession
from observability.redact import redact

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready(db: DbSession) -> dict:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        # A DB connection failure's message can echo the DSN a driver was
        # given (some drivers include it in OperationalError text) -- redact
        # defensively since this detail is client-facing, not just logged.
        raise HTTPException(
            status_code=503, detail=f"database not ready: {redact(str(exc))}"
        ) from exc
    return {"status": "ready"}
