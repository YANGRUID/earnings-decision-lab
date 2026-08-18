"""Collects and persists real analyst-consensus data for a company's next
unreported earnings period. See providers/alpha_vantage_estimates.py and
models/earnings_estimate_snapshot.py for why this is keyed by fiscal period
end date rather than a (fiscal_year, fiscal_quarter) pair, and
docs/engineering_decisions.md (Phase 12) for the full design rationale.
"""

import time
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from analytics.earnings.estimate_revisions import (
    eps_revision_direction,
    revenue_revision_direction,
)
from models.company import Company
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.enums import RevisionDirection, UpcomingEarningsDateSource
from providers.base import EarningsEstimatesProvider
from providers.types import EarningsEstimatePeriod, UpcomingEarningsCalendarEntry


def _select_matching_period(
    all_periods: list[EarningsEstimatePeriod],
    calendar_entry: UpcomingEarningsCalendarEntry,
) -> EarningsEstimatePeriod | None:
    """When a company's next reporting period is its fiscal Q4/year-end,
    Alpha Vantage returns *two* real entries with the same
    fiscal_period_end_date -- one horizon="fiscal quarter", one
    horizon="fiscal year" (observed live: Micron's real EARNINGS_ESTIMATES
    response for FY2026 Q4/FYE, both dated 2026-08-31). Blindly taking the
    first match by date alone is order-dependent on Alpha Vantage's own
    array ordering and can silently substitute the *annual* consensus for
    what this project represents as "next quarterly report" -- a real bug
    caught by live verification, not a hypothetical. This prefers an exact
    "quarter" horizon match at that date, falling back to any horizon match
    (still real data, honestly labeled via the persisted ``horizon`` field)
    only when no quarterly entry exists for that exact date at all.
    """
    same_date = [
        p for p in all_periods if p.fiscal_period_end_date == calendar_entry.fiscal_period_end_date
    ]
    quarterly = next((p for p in same_date if "quarter" in p.horizon.lower()), None)
    return quarterly or (same_date[0] if same_date else None)


def collect_next_earnings_estimate(
    db: Session, provider: EarningsEstimatesProvider, company: Company
) -> EarningsEstimateSnapshot | None:
    """Fetches the provider's real next-report-date prediction and the
    matching detailed consensus (if the provider has one for that exact
    fiscal period), persists one new snapshot row, and returns it.

    Returns ``None`` only when the provider itself reports nothing upcoming
    -- never fabricates a period. If the provider knows a report date but
    has no detailed estimate for that exact period end date, the snapshot
    is still persisted with the real report date and null estimate fields,
    rather than silently skipped -- a real date is real information even
    without a consensus number attached.
    """
    calendar_entry = provider.get_next_earnings_date(company.ticker)
    if calendar_entry is None:
        return None

    # Alpha Vantage's free tier enforces a real per-second burst limit
    # (observed live: two calls fired back-to-back return a 200 OK "please
    # spread out your requests" body, not a retryable 429) on top of its
    # daily cap -- this pause is what keeps these two calls, which always
    # fire together for one ticker, from tripping it.
    time.sleep(1.5)
    all_periods = provider.get_earnings_estimates(company.ticker)
    matching = _select_matching_period(all_periods, calendar_entry)

    previous = (
        db.query(EarningsEstimateSnapshot)
        .filter(
            EarningsEstimateSnapshot.company_id == company.id,
            EarningsEstimateSnapshot.fiscal_period_end_date
            == calendar_entry.fiscal_period_end_date,
        )
        .order_by(EarningsEstimateSnapshot.snapshot_timestamp.desc())
        .first()
    )

    snapshot_timestamp = datetime.now(UTC)
    row = EarningsEstimateSnapshot(
        company_id=company.id,
        fiscal_period_end_date=calendar_entry.fiscal_period_end_date,
        horizon=matching.horizon if matching else "unknown",
        snapshot_timestamp=snapshot_timestamp,
        eps_estimate_average=matching.eps_estimate_average if matching else None,
        eps_estimate_high=matching.eps_estimate_high if matching else None,
        eps_estimate_low=matching.eps_estimate_low if matching else None,
        eps_estimate_analyst_count=matching.eps_estimate_analyst_count if matching else None,
        eps_estimate_revision_up_30d=matching.eps_estimate_revision_up_30d if matching else None,
        eps_estimate_revision_down_30d=matching.eps_estimate_revision_down_30d
        if matching
        else None,
        eps_revision_direction=eps_revision_direction(
            matching.eps_estimate_revision_up_30d if matching else None,
            matching.eps_estimate_revision_down_30d if matching else None,
        ),
        revenue_estimate_average=matching.revenue_estimate_average if matching else None,
        revenue_estimate_high=matching.revenue_estimate_high if matching else None,
        revenue_estimate_low=matching.revenue_estimate_low if matching else None,
        revenue_estimate_analyst_count=matching.revenue_estimate_analyst_count
        if matching
        else None,
        revenue_revision_direction=revenue_revision_direction(
            matching.revenue_estimate_average if matching else None,
            previous.revenue_estimate_average if previous else None,
        ),
        estimated_report_date=calendar_entry.estimated_report_date,
        source_provider="alpha_vantage",
        retrieved_at=snapshot_timestamp,
    )
    db.add(row)
    db.commit()
    return row


def set_manual_earnings_date(
    db: Session,
    company: Company,
    estimated_report_date: date,
    fiscal_period_end_date: date | None = None,
) -> EarningsEstimateSnapshot:
    """Owner/admin action: records ``company``'s next earnings report date
    by hand, for when no provider has published one yet (or to correct a
    provider's date). Persisted with date_source=MANUAL and every
    consensus field (EPS/revenue estimate, analyst count, revision
    direction) null -- a manual date is not analyst consensus and must
    never be read as one. Never overwrites or relabels an existing row;
    always inserts a new snapshot, exactly like a real provider collection
    would, so ``get_latest_earnings_estimate`` picks it up through the same
    ordering every other snapshot uses.

    ``fiscal_period_end_date`` defaults to ``estimated_report_date`` itself
    when not given -- a real, non-fabricated placeholder (no fiscal-period
    consensus is being claimed to match it either way; that field only
    matters for matching against detailed EARNINGS_ESTIMATES rows, which a
    manual entry has none of).

    Callers are responsible for validating ``estimated_report_date`` (e.g.
    that it isn't in the past) before calling this -- see
    api/routers/research.py's set_earnings_date_override.
    """
    snapshot_timestamp = datetime.now(UTC)
    row = EarningsEstimateSnapshot(
        company_id=company.id,
        fiscal_period_end_date=fiscal_period_end_date or estimated_report_date,
        horizon="manual",
        snapshot_timestamp=snapshot_timestamp,
        eps_revision_direction=RevisionDirection.UNKNOWN,
        revenue_revision_direction=RevisionDirection.UNKNOWN,
        estimated_report_date=estimated_report_date,
        date_source=UpcomingEarningsDateSource.MANUAL,
        source_provider="manual",
        retrieved_at=snapshot_timestamp,
    )
    db.add(row)
    db.commit()
    return row


def get_latest_earnings_estimate(db: Session, company_id: int) -> EarningsEstimateSnapshot | None:
    """Most recently collected snapshot for a company's next unreported
    period -- whichever fiscal_period_end_date that currently is."""
    return (
        db.query(EarningsEstimateSnapshot)
        .filter(EarningsEstimateSnapshot.company_id == company_id)
        .order_by(
            EarningsEstimateSnapshot.fiscal_period_end_date.desc(),
            EarningsEstimateSnapshot.snapshot_timestamp.desc(),
        )
        .first()
    )
