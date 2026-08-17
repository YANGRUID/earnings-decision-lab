from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from analytics.options.implied_move import calculate_atm_straddle_implied_move
from providers.types import OptionQuote


class ImpliedMoveArgs(BaseModel):
    underlying_price: Decimal
    strike: Decimal = Field(description="ATM (or nearest available) strike used for the straddle.")
    call_price: Decimal = Field(description="Call mid or last price at that strike.")
    put_price: Decimal = Field(description="Put mid or last price at that strike.")
    expiration_label: str = Field(
        default="unspecified", description="Human-readable expiration, e.g. '2026-09-19'."
    )


class ImpliedMoveTool(Tool[ImpliedMoveArgs]):
    name = "calculate_implied_move"
    description = (
        "Computes the ATM-straddle implied earnings move from explicit call/put prices and "
        "underlying price you supply. This project has no live historical options-chain "
        "provider wired up (see docs/data_sources.md), so this tool works from quotes given "
        "directly in the question, not from a live chain lookup."
    )
    args_schema = ImpliedMoveArgs

    def run(self, args: ImpliedMoveArgs) -> ToolOutcome:
        quotes = [
            OptionQuote(
                ticker="USER_SUPPLIED",
                snapshot_timestamp=datetime.now(UTC),
                expiration_date=date(2099, 1, 1),
                strike=args.strike,
                option_type="call",
                last_price=args.call_price,
                source_provider="user_supplied",
                retrieved_at=datetime.now(UTC),
            ),
            OptionQuote(
                ticker="USER_SUPPLIED",
                snapshot_timestamp=datetime.now(UTC),
                expiration_date=date(2099, 1, 1),
                strike=args.strike,
                option_type="put",
                last_price=args.put_price,
                source_provider="user_supplied",
                retrieved_at=datetime.now(UTC),
            ),
        ]
        result = calculate_atm_straddle_implied_move(
            quotes,
            expiration=date(2099, 1, 1),
            underlying_price=args.underlying_price,
            computed_at=datetime.now(UTC),
        )

        return ToolOutcome(
            success=True,
            summary=(
                f"ATM straddle implied move: {result.implied_move_pct:.2%} "
                f"({result.implied_move_absolute} on {args.underlying_price})."
            ),
            data={
                "method": result.method,
                "implied_move_pct": str(result.implied_move_pct),
                "implied_move_absolute": str(result.implied_move_absolute),
                "expiration_label": args.expiration_label,
            },
        )
