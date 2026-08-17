from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from analytics.earnings.guidance_comparison import compare_guidance
from models.ai_extraction import AIExtraction
from models.company import Company
from models.filing import Filing
from schemas.extraction import GuidanceExtraction
from services.extraction import EXTRACTION_TYPE_GUIDANCE


class GuidanceComparisonArgs(BaseModel):
    ticker: str = Field(description="Stock ticker, e.g. 'MU'")


class GuidanceComparisonTool(Tool):
    name = "compare_guidance"
    description = (
        "Deterministic quarter-over-quarter comparison of a ticker's two most recent "
        "already-extracted guidance figures (revenue, EPS, gross margin, capex midpoint "
        "change). Requires prior extraction runs to exist for that ticker — does not run a "
        "new extraction inline."
    )
    args_schema = GuidanceComparisonArgs

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self, args: GuidanceComparisonArgs) -> ToolOutcome:
        company = (
            self._db.query(Company).filter(Company.ticker == args.ticker.upper()).one_or_none()
        )
        if company is None:
            return ToolOutcome(
                success=True,
                summary=f"No covered company found for ticker {args.ticker!r}.",
                data={},
            )

        rows = (
            self._db.query(AIExtraction, Filing)
            .join(Filing, Filing.id == AIExtraction.filing_id)
            .filter(
                AIExtraction.company_id == company.id,
                AIExtraction.extraction_type == EXTRACTION_TYPE_GUIDANCE,
            )
            .order_by(Filing.filing_date.desc())
            .limit(2)
            .all()
        )

        if len(rows) < 2:
            return ToolOutcome(
                success=True,
                summary=(
                    f"Only {len(rows)} guidance extraction(s) exist for {args.ticker.upper()} — "
                    "need at least 2 to compare. No historical guidance-extraction backfill has "
                    "been run for this ticker yet."
                ),
                data={"available_extractions": len(rows)},
            )

        (current_row, current_filing), (previous_row, previous_filing) = rows
        current = GuidanceExtraction.model_validate(current_row.extracted_data)
        previous = GuidanceExtraction.model_validate(previous_row.extracted_data)
        comparison = compare_guidance(previous, current)

        def _opt(value):
            return str(value) if value is not None else None

        def _range_dict(rc):
            return {
                "previous_midpoint": _opt(rc.previous_midpoint),
                "current_midpoint": _opt(rc.current_midpoint),
                "midpoint_change": _opt(rc.midpoint_change),
                "midpoint_change_pct": _opt(rc.midpoint_change_pct),
            }

        return ToolOutcome(
            success=True,
            summary=(
                f"Compared guidance between {previous_filing.filing_date} and "
                f"{current_filing.filing_date} filings for {args.ticker.upper()}."
            ),
            data={
                "previous_filing_date": previous_filing.filing_date.isoformat(),
                "current_filing_date": current_filing.filing_date.isoformat(),
                "revenue": _range_dict(comparison.revenue),
                "eps": _range_dict(comparison.eps),
                "gross_margin": _range_dict(comparison.gross_margin),
                "capex": _range_dict(comparison.capex),
                "current_key_drivers": current.key_drivers,
                "previous_key_drivers": previous.key_drivers,
            },
        )
