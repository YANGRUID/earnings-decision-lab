"""Authorized end-of-day V4 settlement recovery -- the ONE write-capable
operator route in this deployment.

Deliberately its own module rather than an addition to api/routers/
tws_diagnostics.py: that router's read-only-by-construction guarantee (it
has exactly one HTTP method, GET, and opens no database session) is a real
safety property with a test pinning it, and a recovery endpoint that
appends settlement rows does not belong behind it.

Gating, in layers:
  * the router is registered only when ENABLE_INTERNAL_DIAGNOSTICS is on,
    which is off in normal operation -- the route does not exist at all;
  * every call is a dry run unless the caller passes confirm=APPEND;
  * the service beneath it is append-only and refuses any configuration
    that already has a settled row, so a repeat call is a no-op.

No order path is reachable from here; order placement stays blocked at the
ibapi client level regardless.
"""

from datetime import UTC, date, datetime

from fastapi import APIRouter

from api.deps import DbSession, TwsProviderDep
from api.exceptions import InvalidRequestError

router = APIRouter(prefix="/internal/v4-settlement", tags=["v4-settlement-recovery"])


@router.post("/eod-recovery", response_model=None)
def eod_settlement_recovery(
    db: DbSession,
    tws_provider: TwsProviderDep,
    session_date: str,
    confirm: str = "",
) -> dict:
    """Re-price and settle the configurations whose scheduled settlement
    attempt ran on ``session_date`` and never reached SETTLED, using the
    end-of-day fallback hierarchy in services/v4_settlement_fallback.py."""
    from services.v4_emergency_settlement import recover_due_settlements  # noqa: PLC0415

    if tws_provider is None:
        raise InvalidRequestError("no shared TWS provider on this process")
    try:
        target = date.fromisoformat(session_date)
    except ValueError as exc:
        raise InvalidRequestError("session_date must be YYYY-MM-DD") from exc

    summary = recover_due_settlements(
        db,
        provider=tws_provider,
        session_date=target,
        now=datetime.now(UTC),
        dry_run=confirm != "APPEND",
    )
    return {
        "session_date": summary.session_date.isoformat() if summary.session_date else None,
        "dry_run": summary.dry_run,
        "candidates_considered": summary.candidates_considered,
        "unique_contracts": summary.unique_contracts,
        "quote_calls": summary.quote_calls,
        "close_lookups": summary.close_lookups,
        "settled": summary.settled,
        "unresolved": summary.unresolved,
        "notes": summary.notes,
        "candidates": summary.candidate_rows,
        "configurations": summary.config_rows,
    }
