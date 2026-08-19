"""Owner-only provider configuration: real status, real primary/fallback
selection, real test-connection checks. See services/provider_status.py,
services/provider_settings.py, services/provider_test_connection.py.

No auth system exists yet for this local-first phase (see
docs/engineering_decisions.md) -- these endpoints are reachable by anyone
who can reach the API, same as every other endpoint today. That's a
deliberate, temporary boundary: this router is where a future Azure
deployment would add real owner authentication first, before anything
else, since provider settings can change which paid APIs get called.
"""

from datetime import UTC, datetime

from fastapi import APIRouter

from api.deps import DbSession
from api.exceptions import InvalidRequestError, NotFoundError
from core.config import get_settings
from schemas.api import (
    DomainStatusResponse,
    ProviderCredentialUpdateRequest,
    ProviderDashboardResponse,
    ProviderSettingsUpdateRequest,
    TestConnectionResponse,
)
from services.provider_credentials import (
    InvalidCredentialError,
    UnknownCredentialProviderError,
    delete_provider_credential,
    set_provider_credential,
)
from services.provider_settings import (
    ProviderSettingsUpdate,
    UnknownProviderSelectionError,
    get_strategy_risk_preference,
)
from services.provider_settings import update_app_provider_settings as _update_settings
from services.provider_status import DOMAIN_PROVIDERS, get_provider_dashboard, record_health_event
from services.provider_test_connection import UnknownTestConnectionTargetError, test_connection
from services.secret_store.encryption import MasterKeyNotConfiguredError

router = APIRouter(prefix="/settings/providers", tags=["provider-settings"])


def _dashboard(db: DbSession) -> ProviderDashboardResponse:
    domains = get_provider_dashboard(db, get_settings())
    return ProviderDashboardResponse(
        domains=[DomainStatusResponse.model_validate(d) for d in domains],
        strategy_risk_preference=get_strategy_risk_preference(db).value,
    )


@router.get("", response_model=ProviderDashboardResponse)
def get_dashboard(db: DbSession) -> ProviderDashboardResponse:
    return _dashboard(db)


@router.put("", response_model=ProviderDashboardResponse)
def update_settings(
    request: ProviderSettingsUpdateRequest, db: DbSession
) -> ProviderDashboardResponse:
    update = ProviderSettingsUpdate(
        price_history_primary=request.price_history_primary,
        price_history_fallback=request.price_history_fallback,
        clear_price_history_fallback=request.clear_price_history_fallback,
        options_primary=request.options_primary,
        options_fallback=request.options_fallback,
        clear_options_fallback=request.clear_options_fallback,
        llm_provider=request.llm_provider,
        llm_model=request.llm_model,
        strategy_risk_preference=request.strategy_risk_preference,
    )
    try:
        _update_settings(db, update)
    except UnknownProviderSelectionError as exc:
        raise InvalidRequestError(str(exc)) from exc

    return _dashboard(db)


@router.put("/{provider}/credential", response_model=ProviderDashboardResponse)
def set_credential(
    provider: str, request: ProviderCredentialUpdateRequest, db: DbSession
) -> ProviderDashboardResponse:
    """Add or replace a stored API key -- the actual key is never returned
    in this or any other response, only the resulting configured/masked
    status (see ProviderDashboardResponse). See services/secret_store/ for
    the encrypted-storage design."""
    try:
        set_provider_credential(
            db, get_settings(), provider, request.api_key, request.base_url, request.model
        )
    except UnknownCredentialProviderError as exc:
        raise NotFoundError(str(exc)) from exc
    except InvalidCredentialError as exc:
        raise InvalidRequestError(str(exc)) from exc
    except MasterKeyNotConfiguredError as exc:
        raise InvalidRequestError(str(exc)) from exc

    return _dashboard(db)


@router.delete("/{provider}/credential", response_model=ProviderDashboardResponse)
def remove_credential(provider: str, db: DbSession) -> ProviderDashboardResponse:
    try:
        delete_provider_credential(db, get_settings(), provider)
    except UnknownCredentialProviderError as exc:
        raise NotFoundError(str(exc)) from exc

    return _dashboard(db)


@router.post("/{domain}/{provider}/test", response_model=TestConnectionResponse)
def test_provider_connection(domain: str, provider: str, db: DbSession) -> TestConnectionResponse:
    if domain not in DOMAIN_PROVIDERS or provider not in DOMAIN_PROVIDERS[domain]:
        raise NotFoundError(f"{provider!r} is not a real provider for domain {domain!r}")

    try:
        status, detail = test_connection(get_settings(), provider, domain, db)
    except UnknownTestConnectionTargetError as exc:
        raise NotFoundError(str(exc)) from exc

    tested_at = datetime.now(UTC)
    record_health_event(db, provider, domain, status, detail, tested_at)

    return TestConnectionResponse(
        provider=provider, domain=domain, status=status.value, detail=detail, tested_at=tested_at
    )
