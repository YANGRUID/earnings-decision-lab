"""Owner-managed provider credential CRUD -- the only place credentials are
mutated from the API layer. Validates against the same real-adapter list
every other credential-aware module shares (CREDENTIAL_PROVIDERS: Tiingo,
Alpha Vantage, DeepSeek, OpenAI, Anthropic, OpenAI-compatible) so an
unsupported/made-up provider name is rejected rather than silently
accepted -- SEC EDGAR (no key concept) and IBKR (local Gateway session,
never a stored key) deliberately have no credential form at all.
"""

from sqlalchemy.orm import Session

from core.config import Settings
from services.secret_store import CREDENTIAL_PROVIDERS, LocalEncryptedSecretStore

# Informational grouping only, for the credential row's `domain` column --
# Alpha Vantage in particular backs three domains (price_history,
# earnings_estimates, options) with the one key; this doesn't restrict
# which domains actually use the key, see providers/factory.py.
_PROVIDER_DOMAIN: dict[str, str] = {
    "tiingo": "price_history",
    "alpha_vantage": "price_history",
    "deepseek": "llm",
    "openai": "llm",
    "anthropic": "llm",
    "openai_compatible": "llm",
}


class UnknownCredentialProviderError(Exception):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(
            f"{provider!r} is not a real provider with a credential form -- expected one of "
            f"{CREDENTIAL_PROVIDERS}"
        )


class InvalidCredentialError(Exception):
    pass


def set_provider_credential(
    db: Session,
    settings: Settings,
    provider: str,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
) -> None:
    """Add or replace a credential. Raises MasterKeyNotConfiguredError
    (services.secret_store.encryption) unchanged if SECRET_STORE_MASTER_KEY
    isn't set -- the caller decides how to present that to the owner."""
    if provider not in CREDENTIAL_PROVIDERS:
        raise UnknownCredentialProviderError(provider)
    if not api_key or not api_key.strip():
        raise InvalidCredentialError("API key must not be empty")
    if provider == "openai_compatible" and not (base_url and base_url.strip()):
        raise InvalidCredentialError("OpenAI-compatible requires a base URL")

    extra: dict = {}
    if base_url is not None:
        extra["base_url"] = base_url.strip()
    if model is not None:
        extra["model"] = model.strip()

    store = LocalEncryptedSecretStore(db, settings)
    store.set(provider, _PROVIDER_DOMAIN[provider], api_key.strip(), extra=extra or None)


def delete_provider_credential(db: Session, settings: Settings, provider: str) -> bool:
    if provider not in CREDENTIAL_PROVIDERS:
        raise UnknownCredentialProviderError(provider)
    return LocalEncryptedSecretStore(db, settings).delete(provider)
