"""Collects and persists real analyst-consensus data for a company's next
unreported earnings period. See providers/alpha_vantage_estimates.py and
models/earnings_estimate_snapshot.py for why this is keyed by fiscal period
end date rather than a (fiscal_year, fiscal_quarter) pair, and
docs/engineering_decisions.md (Phase 12) for the full design rationale.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from analytics.earnings.estimate_revisions import (
    eps_revision_direction,
    revenue_revision_direction,
)
from models.company import Company
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from providers.base import EarningsEstimatesProvider


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

    all_periods = provider.get_earnings_estimates(company.ticker)
    matching = next(
        (
            p
            for p in all_periods
            if p.fiscal_period_end_date == calendar_entry.fiscal_period_end_date
        ),
        None,
    )

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
