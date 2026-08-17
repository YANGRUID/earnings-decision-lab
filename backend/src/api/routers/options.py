from fastapi import APIRouter

from agents.tools.implied_move import ImpliedMoveArgs, ImpliedMoveTool
from agents.tools.options_payoff import OptionsPayoffArgs, OptionsPayoffTool

router = APIRouter(prefix="/options", tags=["options"])


@router.post("/strategies/payoff")
def calculate_payoff(args: OptionsPayoffArgs) -> dict:
    outcome = OptionsPayoffTool().run(args)
    return {"summary": outcome.summary, **outcome.data}


@router.post("/implied-move")
def calculate_implied_move(args: ImpliedMoveArgs) -> dict:
    outcome = ImpliedMoveTool().run(args)
    return {"summary": outcome.summary, **outcome.data}
