from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from agents.tools.base import Tool
from agents.tools.types import ToolOutcome
from analytics.options.payoff import Action, OptionLeg, analyze
from models.enums import OptionType


class OptionLegInput(BaseModel):
    option_type: Literal["call", "put"]
    action: Literal["buy", "sell"]
    strike: Decimal
    premium: Decimal
    quantity: int = Field(default=1, ge=1)


class OptionsPayoffArgs(BaseModel):
    strategy_label: str = Field(description="Free-form label, e.g. 'bull call spread'.")
    legs: list[OptionLegInput] = Field(
        min_length=1,
        description="Every leg of the strategy: option type, buy/sell, strike, premium.",
    )


class OptionsPayoffTool(Tool):
    name = "calculate_strategy_payoff"
    description = (
        "Deterministic options strategy payoff calculator: given explicit legs "
        "(type/action/strike/premium), returns net premium, max profit, max loss, and "
        "breakevens. Pure calculation — works for any strikes/premiums supplied, does not "
        "require live market data."
    )
    args_schema = OptionsPayoffArgs

    def run(self, args: OptionsPayoffArgs) -> ToolOutcome:
        legs = [
            OptionLeg(
                option_type=OptionType.CALL if leg.option_type == "call" else OptionType.PUT,
                action=Action.BUY if leg.action == "buy" else Action.SELL,
                strike=leg.strike,
                premium=leg.premium,
                quantity=leg.quantity,
            )
            for leg in args.legs
        ]
        result = analyze(legs)

        return ToolOutcome(
            success=True,
            summary=(
                f"{args.strategy_label}: net {'debit' if result.net_premium >= 0 else 'credit'} "
                f"{abs(result.net_premium)}, max profit "
                f"{'unbounded' if result.max_profit is None else result.max_profit}, max loss "
                f"{'unbounded' if result.max_loss is None else result.max_loss}."
            ),
            data={
                "net_premium": str(result.net_premium),
                "max_profit": (
                    "unbounded" if result.max_profit is None else str(result.max_profit)
                ),
                "max_loss": "unbounded" if result.max_loss is None else str(result.max_loss),
                "breakevens": [str(b) for b in result.breakevens],
            },
        )
