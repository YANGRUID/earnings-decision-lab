"""Phase 4 AI Research evidence-coverage hardening (2026-08-26), Section
27 -- real analyst consensus EPS/revenue estimate data already collected
by Research Preparation (services/market_expectations.py) had no AI
Research tool exposing it at all until now. The live INTU verification
in an earlier pass honestly reported guidance comparison / analyst
consensus as unavailable from the evidence actually used -- this was
true for consensus specifically (compare_guidance already existed for
extracted-filing guidance): no tool ever queried EarningsEstimateSnapshot.

Every field returned is either a real value already collected from a
real provider, or explicitly reported as unavailable -- never a guessed
or interpolated consensus number. This tool distinguishes AVAILABLE DATA
from UNAVAILABLE DATA structurally (a None field, reported as such) so
the LLM has no way to blur the two into an invented analyst consensus.
"""

from datetime import UTC, date, datetime

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from models.company import Company
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot


class EarningsEstimatesArgs(BaseModel):
    ticker: str = Field(description="Stock ticker, e.g. 'INTU'")
    # Point-in-time safety (Phase 4 hardening, 2026-08-26, Section 20-21)
    # -- mirrors earnings_history.py/filings_search.py/guidance_
    # comparison.py's own as_of field exactly. None (the default, every
    # real caller today) means "as of now".
    as_of: date | None = Field(
        default=None,
        description="Optional point-in-time cutoff (YYYY-MM-DD) -- excludes estimate snapshots "
        "collected after this date.",
    )


class EarningsEstimatesTool(Tool[EarningsEstimatesArgs]):
    name = "get_analyst_estimates"
    description = (
        "Real analyst consensus EPS/revenue estimates for a company's next unreported earnings "
        "period, when already collected by this project's own research-preparation pipeline "
        "(estimate averages/high/low, analyst count, 30-day revision direction). Reports each "
        "field as unavailable, never guessed, when the underlying provider didn't supply it. "
        "Not limited to a fixed ticker list -- pass the real ticker the question is about."
    )
    args_schema = EarningsEstimatesArgs

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self, args: EarningsEstimatesArgs) -> ToolOutcome:
        company = (
            self._db.query(Company).filter(Company.ticker == args.ticker.upper()).one_or_none()
        )
        if company is None:
            return ToolOutcome(
                success=True,
                summary=f"No covered company found for ticker {args.ticker!r}.",
                data={},
            )

        query = self._db.query(EarningsEstimateSnapshot).filter(
            EarningsEstimateSnapshot.company_id == company.id
        )
        if args.as_of is not None:
            as_of_end = datetime.combine(args.as_of, datetime.max.time(), tzinfo=UTC)
            query = query.filter(EarningsEstimateSnapshot.snapshot_timestamp <= as_of_end)
        snapshot = query.order_by(
            EarningsEstimateSnapshot.fiscal_period_end_date.desc(),
            EarningsEstimateSnapshot.snapshot_timestamp.desc(),
        ).first()

        if snapshot is None:
            return ToolOutcome(
                success=True,
                summary=(
                    f"No analyst consensus estimate has been collected for "
                    f"{args.ticker.upper()} -- this data is genuinely unavailable, not "
                    "estimated."
                ),
                data={"available": False},
            )

        def _opt(value):
            return str(value) if value is not None else None

        return ToolOutcome(
            success=True,
            summary=(
                f"Real analyst consensus estimates for {args.ticker.upper()}'s "
                f"{snapshot.fiscal_period_end_date} period, collected {snapshot.snapshot_timestamp}"
                f" from {snapshot.source_provider}."
            ),
            data={
                "available": True,
                "fiscal_period_end_date": snapshot.fiscal_period_end_date.isoformat(),
                "estimated_report_date": (
                    snapshot.estimated_report_date.isoformat()
                    if snapshot.estimated_report_date
                    else None
                ),
                "snapshot_timestamp": snapshot.snapshot_timestamp.isoformat(),
                "eps_estimate_average": _opt(snapshot.eps_estimate_average),
                "eps_estimate_high": _opt(snapshot.eps_estimate_high),
                "eps_estimate_low": _opt(snapshot.eps_estimate_low),
                "eps_estimate_analyst_count": snapshot.eps_estimate_analyst_count,
                "eps_revision_direction_30d": snapshot.eps_revision_direction.value,
                "revenue_estimate_average": _opt(snapshot.revenue_estimate_average),
                "revenue_estimate_high": _opt(snapshot.revenue_estimate_high),
                "revenue_estimate_low": _opt(snapshot.revenue_estimate_low),
                "revenue_estimate_analyst_count": snapshot.revenue_estimate_analyst_count,
                "revenue_revision_direction_30d": snapshot.revenue_revision_direction.value,
                "source_provider": snapshot.source_provider,
            },
        )
