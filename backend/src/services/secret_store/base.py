from typing import Protocol


class SecretStore(Protocol):
    """Read-side contract every secret store implements. Only
    LocalEncryptedSecretStore also supports set/delete today -- a read-only
    store (env vars now, a cloud key vault later) legitimately has nothing
    to mutate, so those methods are deliberately not part of this shared
    Protocol.
    """

    def get(self, provider: str) -> str | None: ...

    def configured(self, provider: str) -> bool: ...

    def masked(self, provider: str) -> str | None: ...
