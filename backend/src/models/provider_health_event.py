from datetime import datetime

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.enums import ProviderHealthStatus


class ProviderHealthEvent(Base):
    """An append-only log of real provider check outcomes -- written by an
    explicit "Test Connection" call (services/provider_status.py) and by
    real ingestion failures (research_orchestration.py's optional steps),
    never synthesized. ``last_success_at`` for a provider is deliberately
    NOT derived from this table -- it's derived from real ingested data
    (e.g. the latest PriceBar.retrieved_at for a given source_provider),
    which is a stronger, already-real signal than a log row could be. This
    table exists specifically for the thing no other table already
    captures: *failures*, and explicit connectivity tests.
    """

    __tablename__ = "provider_health_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[ProviderHealthStatus] = mapped_column(
        Enum(ProviderHealthStatus, name="provider_health_status")
    )
    detail: Mapped[str | None] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
