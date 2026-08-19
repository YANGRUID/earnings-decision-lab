from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ProviderUsageEvent(Base):
    """One normalized row per real call made through a provider adapter --
    written centrally by services/usage_instrumentation.py, never scattered
    across individual endpoints (see Phase 14.10 Part L). Covers both LLM
    calls (input_tokens/output_tokens/total_tokens, only when the provider's
    own response actually reports them -- never estimated) and data-API
    calls (provider_units -- e.g. "1 request"). This table is the entire
    basis for the API Usage dashboard; it is never read for anything else,
    so it deliberately stores nothing an attacker or a stray log line could
    turn into a credential: no API keys, no authorization headers, no IBKR
    session cookies, no request/response bodies.
    """

    __tablename__ = "provider_usage_event"

    id: Mapped[int] = mapped_column(primary_key=True)

    provider: Mapped[str] = mapped_column(String(32), index=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)
    operation: Mapped[str] = mapped_column(String(64))

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    success: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status_code: Mapped[str | None] = mapped_column(String(32))
    rate_limited: Mapped[bool] = mapped_column(Boolean, default=False)

    # LLM-only -- null for data-API events, and null even for an LLM event
    # when the provider's response genuinely didn't report a token count
    # (never backfilled with an estimate; see services/usage_instrumentation.py).
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)

    # Data-API-only -- the provider's own unit of billing/quota (e.g. "1
    # request"), when meaningfully distinct from "one row in this table".
    provider_units: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))

    # Only ever populated from a real, provider-reported cost or an
    # explicit, owner-configured price-per-unit -- see
    # services/usage_cost.py. Null means "cost unavailable", never a
    # fabricated number.
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
