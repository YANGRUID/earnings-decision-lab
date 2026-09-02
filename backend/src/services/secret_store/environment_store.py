from core.config import Settings
from services.secret_store.masking import mask_secret

# The only place this project maps a provider name to its env-var field --
# deliberately small and hand-maintained (mirrors
# services/provider_status.py::_raw_key, which this module supersedes).
_ENV_ATTR: dict[str, str] = {
    "tiingo": "tiingo_api_key",
    "alpha_vantage": "alpha_vantage_api_key",
    "deepseek": "deepseek_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "openai_compatible": "openai_compatible_api_key",
    # Not yet in services.secret_store.resolver.CREDENTIAL_PROVIDERS -- no
    # Settings-UI credential form exists for it yet (Phase 4 scope is the
    # provider adapter itself, see providers/finnhub.py), so this is
    # currently env-var-only. Registering it here still makes
    # resolve_secret()'s DB-then-env precedence correct in advance, for
    # whenever that form is added.
    "finnhub": "finnhub_api_key",
    # Same as "finnhub" above -- env-var-only, no Settings-UI form yet
    # (see providers/earningsapi.py). Without this entry,
    # resolve_secret(settings, "earningsapi", db) silently returns None
    # even with EARNINGSAPI_API_KEY set, which would make
    # providers/factory.py::build_earnings_calendar_provider treat
    # EarningsAPI.com as unconfigured.
    "earningsapi": "earningsapi_api_key",
}


class EnvironmentSecretStore:
    """Read-only. Wraps whatever .env/process-env already configured --
    this is the entire credential system this project had before the
    Settings-UI credential forms existed, preserved unchanged as the
    fallback layer under LocalEncryptedSecretStore."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get(self, provider: str) -> str | None:
        attr = _ENV_ATTR.get(provider)
        return getattr(self._settings, attr, None) if attr else None

    def configured(self, provider: str) -> bool:
        return bool(self.get(provider))

    def masked(self, provider: str) -> str | None:
        return mask_secret(self.get(provider))
