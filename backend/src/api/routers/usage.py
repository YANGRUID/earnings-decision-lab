"""API Usage / Costs -- read-only view over ProviderUsageEvent (see
services/usage_instrumentation.py, which records every event centrally at
provider-construction time; this router never writes anything). Owner-only,
same boundary as api/routers/provider_settings.py -- no auth system exists
yet for this local-first phase.
"""

from fastapi import APIRouter, Query

from api.deps import DbSession
from api.exceptions import InvalidRequestError
from schemas.api import ProviderUsageSummaryResponse, UsageSummaryResponse
from services.usage_stats import UsageWindow, compute_usage_summary

router = APIRouter(prefix="/settings/usage", tags=["usage"])

_VALID_WINDOWS: tuple[UsageWindow, ...] = ("today", "7d", "30d", "all_time")


@router.get("", response_model=UsageSummaryResponse)
def get_usage_summary(db: DbSession, window: str = Query("7d")) -> UsageSummaryResponse:
    if window not in _VALID_WINDOWS:
        raise InvalidRequestError(f"window must be one of {_VALID_WINDOWS}, got {window!r}")
    summary = compute_usage_summary(db, window)  # type: ignore[arg-type]
    return UsageSummaryResponse(
        window=summary.window,
        since=summary.since,
        total_requests=summary.total_requests,
        total_errors=summary.total_errors,
        total_rate_limited=summary.total_rate_limited,
        total_llm_tokens=summary.total_llm_tokens,
        total_estimated_cost=summary.total_estimated_cost,
        providers=[ProviderUsageSummaryResponse.model_validate(p) for p in summary.providers],
    )
