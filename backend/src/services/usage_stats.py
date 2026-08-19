"""Aggregation over ProviderUsageEvent for the API Usage dashboard (Phase
14.10 Part C5). Every number here is a real count/sum/average over real
recorded events -- nothing here is estimated, and a field is left ``None``
("unavailable") rather than backfilled with a guess whenever the
underlying events never carried that signal (e.g. estimated_cost, which
this cycle never populates -- see services/usage_instrumentation.py).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy.orm import Session

from models.provider_usage_event import ProviderUsageEvent

UsageWindow = Literal["today", "7d", "30d", "all_time"]


@dataclass(frozen=True)
class ProviderUsageSummary:
    provider: str
    domain: str
    request_count: int
    success_count: int
    error_count: int
    rate_limited_count: int
    avg_latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost: Decimal | None
    last_event_at: datetime | None


@dataclass(frozen=True)
class UsageSummary:
    window: UsageWindow
    since: datetime | None
    total_requests: int
    total_errors: int
    total_rate_limited: int
    total_llm_tokens: int | None
    total_estimated_cost: Decimal | None
    providers: list[ProviderUsageSummary]


def _window_start(window: UsageWindow) -> datetime | None:
    now = datetime.now(UTC)
    if window == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "7d":
        return now - timedelta(days=7)
    if window == "30d":
        return now - timedelta(days=30)
    return None


def compute_usage_summary(db: Session, window: UsageWindow) -> UsageSummary:
    since = _window_start(window)
    query = db.query(ProviderUsageEvent)
    if since is not None:
        query = query.filter(ProviderUsageEvent.occurred_at >= since)
    events = query.all()

    by_key: dict[tuple[str, str], list[ProviderUsageEvent]] = {}
    for event in events:
        by_key.setdefault((event.provider, event.domain), []).append(event)

    providers: list[ProviderUsageSummary] = []
    for (provider, domain), evts in sorted(by_key.items()):
        latencies = [e.latency_ms for e in evts if e.latency_ms is not None]
        input_tokens = [e.input_tokens for e in evts if e.input_tokens is not None]
        output_tokens = [e.output_tokens for e in evts if e.output_tokens is not None]
        total_tokens = [e.total_tokens for e in evts if e.total_tokens is not None]
        costs = [e.estimated_cost for e in evts if e.estimated_cost is not None]
        providers.append(
            ProviderUsageSummary(
                provider=provider,
                domain=domain,
                request_count=len(evts),
                success_count=sum(1 for e in evts if e.success),
                error_count=sum(1 for e in evts if not e.success),
                rate_limited_count=sum(1 for e in evts if e.rate_limited),
                avg_latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
                input_tokens=sum(input_tokens) if input_tokens else None,
                output_tokens=sum(output_tokens) if output_tokens else None,
                total_tokens=sum(total_tokens) if total_tokens else None,
                estimated_cost=sum(costs, Decimal(0)) if costs else None,
                last_event_at=max(e.occurred_at for e in evts),
            )
        )

    all_total_tokens = [p.total_tokens for p in providers if p.total_tokens is not None]
    all_costs = [p.estimated_cost for p in providers if p.estimated_cost is not None]

    return UsageSummary(
        window=window,
        since=since,
        total_requests=len(events),
        total_errors=sum(1 for e in events if not e.success),
        total_rate_limited=sum(1 for e in events if e.rate_limited),
        total_llm_tokens=sum(all_total_tokens) if all_total_tokens else None,
        total_estimated_cost=sum(all_costs, Decimal(0)) if all_costs else None,
        providers=providers,
    )
