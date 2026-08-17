from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from models.company import Company
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.price_reaction import PriceReaction


class EarningsHistoryArgs(BaseModel):
    ticker: str = Field(description="Stock ticker, e.g. 'MU'")
    limit: int = Field(default=8, ge=1, le=20)


class EarningsHistoryTool(Tool[EarningsHistoryArgs]):
    name = "get_historical_earnings"
    description = (
        "Real historical earnings results and price reactions for a covered ticker "
        "(NVDA, AMD, MU, SNDK): actual EPS/revenue and next-day/five-day price moves "
        "where available."
    )
    args_schema = EarningsHistoryArgs

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self, args: EarningsHistoryArgs) -> ToolOutcome:
        company = (
            self._db.query(Company).filter(Company.ticker == args.ticker.upper()).one_or_none()
        )
        if company is None:
            return ToolOutcome(
                success=True,
                summary=f"No covered company found for ticker {args.ticker!r}.",
                data={"events": []},
            )

        stmt = (
            select(EarningsEvent, EarningsResult, PriceReaction)
            .join(
                EarningsResult,
                EarningsResult.earnings_event_id == EarningsEvent.id,
                isouter=True,
            )
            .join(
                PriceReaction,
                PriceReaction.earnings_event_id == EarningsEvent.id,
                isouter=True,
            )
            .where(EarningsEvent.company_id == company.id)
            .order_by(EarningsEvent.fiscal_year.desc(), EarningsEvent.fiscal_quarter.desc())
            .limit(args.limit)
        )
        rows = self._db.execute(stmt).all()

        events = []
        for event, result, reaction in rows:
            events.append(
                {
                    "fiscal_year": event.fiscal_year,
                    "fiscal_quarter": event.fiscal_quarter,
                    "earnings_date": (
                        event.earnings_date.isoformat() if event.earnings_date else None
                    ),
                    "date_confirmed": event.date_confirmed,
                    "actual_eps": str(result.actual_eps) if result and result.actual_eps else None,
                    "actual_revenue": str(result.actual_revenue)
                    if result and result.actual_revenue
                    else None,
                    "next_day_move_pct": str(reaction.next_day_move_pct)
                    if reaction and reaction.next_day_move_pct
                    else None,
                    "five_day_move_pct": str(reaction.five_day_move_pct)
                    if reaction and reaction.five_day_move_pct
                    else None,
                }
            )

        return ToolOutcome(
            success=True,
            summary=f"Found {len(events)} historical earnings events for {args.ticker.upper()}.",
            data={"ticker": args.ticker.upper(), "events": events},
            query_description=str(stmt.compile(compile_kwargs={"literal_binds": True})),
        )
