"""Real system status: data counts, freshness timestamps, AI provider
configuration, IBKR live state, scheduler state, provider health, and
the evaluation summary -- what this deployment actually has, stated
plainly. See services/system_status.py, services/scheduler.py, and
services/provider_status.py.
"""

from fastapi import APIRouter

from analytics.market_session import get_market_session
from api.deps import DbSession, Scheduler
from api.routers.evaluations import latest_evaluation
from core.config import get_settings
from rag.embeddings import DEFAULT_MODEL_NAME
from schemas.api import (
    DataCountsResponse,
    DataFreshnessResponse,
    DomainStatusResponse,
    IbkrStatusResponse,
    LlmConfigStatusResponse,
    ProviderDashboardResponse,
    SchedulerStatusResponse,
    SystemStatusResponse,
)
from services.provider_status import get_provider_dashboard
from services.scheduler import get_scheduler_status
from services.system_status import (
    describe_llm_configuration,
    get_data_counts,
    get_data_freshness,
    get_ibkr_status,
)

router = APIRouter(prefix="/system-status", tags=["system-status"])


@router.get("", response_model=SystemStatusResponse)
def get_system_status(db: DbSession, scheduler: Scheduler) -> SystemStatusResponse:
    settings = get_settings()
    counts = get_data_counts(db)
    freshness = get_data_freshness(db)
    llm = describe_llm_configuration(settings)
    ibkr = get_ibkr_status(settings)
    scheduler_status = get_scheduler_status(scheduler)
    session = get_market_session()
    domains = get_provider_dashboard(db, settings)

    return SystemStatusResponse(
        counts=DataCountsResponse.model_validate(counts),
        freshness=DataFreshnessResponse.model_validate(freshness),
        llm=LlmConfigStatusResponse.model_validate(llm),
        embedding_model=DEFAULT_MODEL_NAME,
        evaluation=latest_evaluation(),
        ibkr=IbkrStatusResponse.model_validate(ibkr),
        scheduler=SchedulerStatusResponse.model_validate(scheduler_status),
        market_session=session.session.value,
        providers=ProviderDashboardResponse(
            domains=[DomainStatusResponse.model_validate(d) for d in domains]
        ),
    )
