from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from models.company import Company
from models.options_snapshot import OptionsSnapshot


class OptionsSnapshotArgs(BaseModel):
    ticker: str = Field(description="Stock ticker, e.g. 'MU'")


class OptionsSnapshotTool(Tool[OptionsSnapshotArgs]):
    name = "get_options_snapshot"
    description = (
        "Real-time-ish options chain snapshot for a ticker, if one has been ingested. "
        "This project currently has no historical options-chain data provider wired up "
        "(see docs/data_sources.md) — this tool queries the real table and reports "
        "honestly if it's empty rather than fabricating a chain."
    )
    args_schema = OptionsSnapshotArgs

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self, args: OptionsSnapshotArgs) -> ToolOutcome:
        company = (
            self._db.query(Company).filter(Company.ticker == args.ticker.upper()).one_or_none()
        )
        if company is None:
            return ToolOutcome(
                success=True,
                summary=f"No covered company found for ticker {args.ticker!r}.",
                data={"snapshots": []},
            )

        count = (
            self._db.query(OptionsSnapshot).filter(OptionsSnapshot.company_id == company.id).count()
        )
        if count == 0:
            return ToolOutcome(
                success=True,
                summary=(
                    f"No options chain data is available for {args.ticker.upper()} — no "
                    "options-data provider is currently configured for this project."
                ),
                data={"snapshots": []},
            )
        # (Not reached today, but real once a provider is wired up.)
        rows = (
            self._db.query(OptionsSnapshot)
            .filter(OptionsSnapshot.company_id == company.id)
            .order_by(OptionsSnapshot.snapshot_timestamp.desc())
            .limit(20)
            .all()
        )
        snapshots = [{"strike": str(r.strike), "type": r.option_type.value} for r in rows]
        return ToolOutcome(
            success=True,
            summary=f"Found {len(rows)} options quotes for {args.ticker.upper()}.",
            data={"snapshots": snapshots},
        )
