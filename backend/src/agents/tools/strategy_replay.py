from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from models.company import Company
from models.strategy_replay import StrategyReplay


class StrategyReplayArgs(BaseModel):
    ticker: str = Field(description="Stock ticker, e.g. 'MU'")


class StrategyReplayTool(Tool[StrategyReplayArgs]):
    name = "run_strategy_replay"
    description = (
        "Historical options-strategy replay results for a ticker's past earnings events "
        "(entry economics, strike-selection rule used, resulting payoff). The replay engine "
        "(analytics/options/replay.py) is implemented and tested, but no historical "
        "options-chain data exists to run it against yet (see docs/earnings_methodology.md) — "
        "this tool queries the real table and reports honestly if it's empty."
    )
    args_schema = StrategyReplayArgs

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self, args: StrategyReplayArgs) -> ToolOutcome:
        company = (
            self._db.query(Company).filter(Company.ticker == args.ticker.upper()).one_or_none()
        )
        if company is None:
            return ToolOutcome(
                success=True,
                summary=f"No covered company found for ticker {args.ticker!r}.",
                data={"replays": []},
            )

        rows = (
            self._db.query(StrategyReplay)
            .join(StrategyReplay.earnings_event)
            .filter(StrategyReplay.earnings_event.has(company_id=company.id))
            .all()
        )
        if not rows:
            return ToolOutcome(
                success=True,
                summary=(
                    f"No historical strategy-replay results exist for {args.ticker.upper()} — "
                    "no historical options-chain data is available to reconstruct one from."
                ),
                data={"replays": []},
            )
        return ToolOutcome(
            success=True,
            summary=f"Found {len(rows)} historical strategy replays for {args.ticker.upper()}.",
            data={"replays": [r.strategy_name for r in rows]},
        )
