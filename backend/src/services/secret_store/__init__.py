"""Owner-managed provider credentials, stored encrypted at rest.

    SecretStore (Protocol)
    ├── EnvironmentSecretStore     -- read-only, wraps core.config.Settings
    ├── LocalEncryptedSecretStore  -- read/write, backed by the
    │                                 provider_credential table + a
    │                                 backend-only Fernet master key
    └── (future) AzureKeyVaultSecretStore

``resolve_secret``/``resolve_extra``/``credential_status`` are the real
entry points every provider factory and the status dashboard should use --
they apply the one resolution rule this project has: an owner-configured
key (LocalEncryptedSecretStore, set through the Settings UI) always wins
over the env-var default (EnvironmentSecretStore) when both exist, so an
env-only deployment that has never touched the credential UI is completely
unaffected.
"""

from services.secret_store.base import SecretStore
from services.secret_store.environment_store import EnvironmentSecretStore
from services.secret_store.local_encrypted_store import LocalEncryptedSecretStore
from services.secret_store.masking import mask_secret
from services.secret_store.resolver import (
    CREDENTIAL_PROVIDERS,
    credential_status,
    resolve_extra,
    resolve_secret,
)

__all__ = [
    "CREDENTIAL_PROVIDERS",
    "EnvironmentSecretStore",
    "LocalEncryptedSecretStore",
    "SecretStore",
    "credential_status",
    "mask_secret",
    "resolve_extra",
    "resolve_secret",
]
