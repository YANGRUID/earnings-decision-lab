from decimal import Decimal

from agents.tools.implied_move import ImpliedMoveArgs, ImpliedMoveTool


def test_calculates_implied_move_from_supplied_quotes():
    tool = ImpliedMoveTool()
    args = ImpliedMoveArgs(
        underlying_price=Decimal("114.50"),
        strike=Decimal("115"),
        call_price=Decimal("4.30"),
        put_price=Decimal("4.10"),
        expiration_label="2025-09-26",
    )

    outcome = tool.run(args)

    assert outcome.success
    assert outcome.data["implied_move_absolute"] == "8.40"
    assert outcome.data["expiration_label"] == "2025-09-26"
    assert "8.40" in outcome.summary
