from sqlalchemy.orm import Session

from core.config import Settings
from models.provider_credential import ProviderCredential
from services.secret_store.encryption import (
    MasterKeyNotConfiguredError,
    SecretDecryptionError,
    decrypt,
    encrypt,
)
from services.secret_store.masking import mask_secret


class LocalEncryptedSecretStore:
    """Backend-managed, encrypted-at-rest credential storage for a local
    deployment -- the "simplest secure local backend-managed approach"
    this cycle calls for, not a full enterprise secrets manager. Backed by
    the provider_credential table; the actual encryption key
    (SECRET_STORE_MASTER_KEY) lives only in this process's environment,
    never in the database itself.

    Reads degrade gracefully to "nothing stored" on any failure (no master
    key configured, wrong master key, corrupted row) so a read never
    crashes a request -- callers always have the env-var fallback. Writes
    raise MasterKeyNotConfiguredError plainly, since an owner actively
    trying to save a key through the UI needs to know why it didn't work.
    """

    def __init__(self, db: Session, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    def _row(self, provider: str) -> ProviderCredential | None:
        return self._db.get(ProviderCredential, provider)

    def get(self, provider: str) -> str | None:
        row = self._row(provider)
        if row is None or row.secret_ciphertext is None:
            return None
        try:
            return decrypt(self._settings, row.secret_ciphertext)
        except (MasterKeyNotConfiguredError, SecretDecryptionError):
            return None

    def configured(self, provider: str) -> bool:
        row = self._row(provider)
        return row is not None and row.secret_ciphertext is not None

    def masked(self, provider: str) -> str | None:
        row = self._row(provider)
        if row is None or row.secret_last4 is None:
            return None
        return "••••••••" + row.secret_last4

    def get_extra(self, provider: str) -> dict:
        row = self._row(provider)
        return dict(row.extra) if row is not None and row.extra else {}

    def set(self, provider: str, domain: str, value: str, extra: dict | None = None) -> None:
        """Raises MasterKeyNotConfiguredError if no master key is set --
        never silently stores a plaintext fallback."""
        ciphertext = encrypt(self._settings, value)
        row = self._row(provider)
        if row is None:
            row = ProviderCredential(provider=provider, domain=domain)
            self._db.add(row)
        row.domain = domain
        row.secret_ciphertext = ciphertext
        row.secret_last4 = value[-4:] if len(value) >= 4 else "*" * len(value)
        if extra is not None:
            row.extra = extra
        self._db.commit()

    def set_extra(self, provider: str, domain: str, extra: dict) -> None:
        """For non-secret per-provider fields (e.g. OpenAI-compatible's
        base_url) that need to persist alongside -- or even without -- a
        stored secret."""
        row = self._row(provider)
        if row is None:
            row = ProviderCredential(provider=provider, domain=domain)
            self._db.add(row)
        row.domain = domain
        merged = dict(row.extra) if row.extra else {}
        merged.update({k: v for k, v in extra.items() if v is not None})
        row.extra = merged
        self._db.commit()

    def delete(self, provider: str) -> bool:
        row = self._row(provider)
        if row is None:
            return False
        self._db.delete(row)
        self._db.commit()
        return True

    def mask_value(self, value: str | None) -> str | None:
        return mask_secret(value)
