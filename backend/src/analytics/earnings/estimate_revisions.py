"""Deterministic derivation of revision direction for stored earnings
estimates. No LLM involvement -- see docs/engineering_decisions.md for why
financial/quantitative derivations are never delegated to a model.

EPS and revenue are derived differently because the underlying data is
genuinely different, not for consistency's sake: Alpha Vantage's
EARNINGS_ESTIMATES response includes real trailing-30-day up/down revision
counts for EPS only, so eps_revision_direction is computed straight from
that single response. It has no equivalent revenue field, so
revenue_revision_direction can only be computed by this project comparing
two of its own stored snapshots over time -- meaning it stays UNKNOWN until
a second real snapshot for the same fiscal period exists.
"""

from decimal import Decimal

from models.enums import RevisionDirection


def eps_revision_direction(
    revision_up_30d: int | None, revision_down_30d: int | None
) -> RevisionDirection:
    """From Alpha Vantage's real trailing-30-day analyst-revision counts."""
    if revision_up_30d is None and revision_down_30d is None:
        return RevisionDirection.UNKNOWN
    up = revision_up_30d or 0
    down = revision_down_30d or 0
    if up > down:
        return RevisionDirection.UP
    if down > up:
        return RevisionDirection.DOWN
    return RevisionDirection.FLAT


def revenue_revision_direction(
    current_average: Decimal | None, previous_average: Decimal | None
) -> RevisionDirection:
    """From comparing this snapshot's consensus against the immediately
    preceding stored snapshot for the same (company, fiscal_period_end_date)
    -- ``previous_average`` is ``None`` on a period's first-ever snapshot.
    """
    if current_average is None or previous_average is None:
        return RevisionDirection.UNKNOWN
    if current_average > previous_average:
        return RevisionDirection.UP
    if current_average < previous_average:
        return RevisionDirection.DOWN
    return RevisionDirection.FLAT
