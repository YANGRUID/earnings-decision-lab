from datetime import datetime

from sqlalchemy import JSON, DateTime, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ProviderCredential(Base):
    """An owner-entered provider credential, stored encrypted -- see
    services/secret_store/. One row per real provider adapter (never a
    made-up provider name; validated in services/provider_credentials.py
    against the same known-provider lists provider_settings.py already
    enforces for provider *selection*).

    ``secret_ciphertext`` is the API key, encrypted with a backend-only
    master key (core.config.Settings.secret_store_master_key) that never
    lives in this database -- a stolen DB dump alone can't recover a key.
    ``secret_last4`` is stored in plaintext specifically so the dashboard
    can render a masked suffix (see services/secret_store/masking.py)
    without ever decrypting the real key just to display it.

    ``extra`` holds small, NON-secret per-provider fields that still need
    to persist alongside a credential -- today only OpenAI-compatible's
    ``base_url``/``model`` (a self-hosted or third-party endpoint's URL is
    not a secret, but has nowhere else to live once it stops being sourced
    from .env).

    A row can exist with ``secret_ciphertext IS NULL`` -- e.g. a provider
    that only ever needed ``extra`` fields set. ``configured`` therefore
    always means "secret_ciphertext is present", never "a row exists".
    """

    __tablename__ = "provider_credential"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    domain: Mapped[str] = mapped_column(String(32))

    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_last4: Mapped[str | None] = mapped_column(String(8))

    extra: Mapped[dict | None] = mapped_column(JSON)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
