"""Real system status: data counts, freshness timestamps, AI provider
configuration, and the evaluation summary -- what this deployment actually
has, stated plainly. See services/system_status.py.
"""

from fastapi import APIRouter

from api.deps import DbSession
from api.routers.evaluations import latest_evaluation
from core.config import get_settings
from rag.embeddings import DEFAULT_MODEL_NAME
from schemas.api import (
    DataCountsResponse,
    DataFreshnessResponse,
    LlmConfigStatusResponse,
    SystemStatusResponse,
)
from services.system_status import describe_llm_configuration, get_data_counts, get_data_freshness

router = APIRouter(prefix="/system-status", tags=["system-status"])


@router.get("", response_model=SystemStatusResponse)
def get_system_status(db: DbSession) -> SystemStatusResponse:
    counts = get_data_counts(db)
    freshness = get_data_freshness(db)
    llm = describe_llm_configuration(get_settings())

    return SystemStatusResponse(
        counts=DataCountsResponse.model_validate(counts),
        freshness=DataFreshnessResponse.model_validate(freshness),
        llm=LlmConfigStatusResponse.model_validate(llm),
        embedding_model=DEFAULT_MODEL_NAME,
        evaluation=latest_evaluation(),
    )
