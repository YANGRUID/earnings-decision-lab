from sqlalchemy.orm import Session

from core.config import Settings
from services.secret_store.environment_store import EnvironmentSecretStore
from services.secret_store.local_encrypted_store import LocalEncryptedSecretStore

# The only real adapters that take a secret API key -- SEC EDGAR (no key
# concept) and IBKR (local Gateway session, never a stored key) are
# deliberately excluded; see services/provider_credentials.py, which is
# where this list is enforced against the credential-write API.
CREDENTIAL_PROVIDERS = (
    "tiingo",
    "alpha_vantage",
    "deepseek",
    "openai",
    "anthropic",
    "openai_compatible",
)


def resolve_secret(settings: Settings, provider: str, db: Session | None = None) -> str | None:
    """The one real key-resolution rule in this project: an owner-
    configured key (stored encrypted, set through the Settings UI) always
    wins over the env-var default when both exist. ``db=None`` (e.g. a
    standalone ingestion script with no request-scoped session) skips the
    DB lookup entirely and behaves exactly as this project always has --
    env-var only."""
    if db is not None:
        stored = LocalEncryptedSecretStore(db, settings).get(provider)
        if stored:
            return stored
    return EnvironmentSecretStore(settings).get(provider)


def resolve_extra(provider: str, settings: Settings, db: Session | None = None) -> dict:
    """Non-secret per-provider fields that still need DB-then-env
    resolution -- today only OpenAI-compatible's base_url/model, which
    have no other home once they stop being sourced purely from .env."""
    stored: dict = {}
    if db is not None:
        stored = LocalEncryptedSecretStore(db, settings).get_extra(provider)
    if provider == "openai_compatible":
        return {
            "base_url": stored.get("base_url") or settings.openai_compatible_base_url,
            "model": stored.get("model") or settings.openai_compatible_model,
        }
    return stored


def credential_status(
    settings: Settings, provider: str, db: Session | None = None
) -> tuple[bool, str | None]:
    """(configured, masked) using the exact same DB-wins-then-env
    precedence as resolve_secret, so a dashboard's "configured" bool never
    disagrees with what a real provider call would actually use."""
    if db is not None:
        local = LocalEncryptedSecretStore(db, settings)
        if local.configured(provider):
            return True, local.masked(provider)
    env = EnvironmentSecretStore(settings)
    return env.configured(provider), env.masked(provider)
