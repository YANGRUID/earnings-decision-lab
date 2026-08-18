from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class AppProviderSettings(Base):
    """Owner-configured provider selection overrides -- a single row
    (id=1), read fresh on every request that constructs a provider (see
    services/provider_settings.py), never cached at process-startup like
    ``core.config.Settings``. When a field is null, the caller falls back
    to the real env-var default (``Settings.options_provider`` etc.) --
    this table only ever *overrides*, it never becomes the sole source of
    truth, so an env-only deployment with no owner interaction behaves
    exactly as it always has.

    Only price_history and options have a real primary/fallback choice
    (more than one real adapter exists for each -- see
    providers/factory.py). Earnings estimates (Alpha Vantage only) and
    filings (SEC EDGAR only) have no real alternative to select, so they
    aren't represented here at all -- never fabricate a choice that
    doesn't exist.
    """

    __tablename__ = "app_provider_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_app_provider_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    price_history_primary: Mapped[str | None] = mapped_column(String(32))
    price_history_fallback: Mapped[str | None] = mapped_column(String(32))

    options_primary: Mapped[str | None] = mapped_column(String(32))
    options_fallback: Mapped[str | None] = mapped_column(String(32))

    llm_provider: Mapped[str | None] = mapped_column(String(32))
    llm_model: Mapped[str | None] = mapped_column(String(128))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
