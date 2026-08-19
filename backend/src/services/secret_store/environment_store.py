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
