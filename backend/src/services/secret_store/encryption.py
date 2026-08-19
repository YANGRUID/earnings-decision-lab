from cryptography.fernet import Fernet, InvalidToken

from core.config import Settings

__all__ = ["MasterKeyNotConfiguredError", "SecretDecryptionError", "decrypt", "encrypt"]


class MasterKeyNotConfiguredError(Exception):
    """Raised only when something tries to WRITE a credential through
    LocalEncryptedSecretStore without SECRET_STORE_MASTER_KEY set. Reading
    never raises this -- an unreadable/never-configured store just means
    "nothing stored", so the caller falls back to the env var, same as
    always."""


class SecretDecryptionError(Exception):
    """The stored ciphertext exists but can't be decrypted with the
    current master key -- e.g. the key was rotated/lost, or the row is
    corrupted. Never surfaced as a crash; callers treat this exactly like
    "nothing stored" and fall back to the env var."""


def _fernet(settings: Settings) -> Fernet:
    key = settings.secret_store_master_key
    if not key:
        raise MasterKeyNotConfiguredError(
            "SECRET_STORE_MASTER_KEY is not set. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())" '
            "and set it in .env before storing credentials through the Settings UI."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise MasterKeyNotConfiguredError(
            "SECRET_STORE_MASTER_KEY is set but is not a valid Fernet key."
        ) from exc


def encrypt(settings: Settings, plaintext: str) -> bytes:
    return _fernet(settings).encrypt(plaintext.encode("utf-8"))


def decrypt(settings: Settings, ciphertext: bytes) -> str:
    try:
        return _fernet(settings).decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptionError(
            "Stored credential could not be decrypted with the current master key."
        ) from exc
