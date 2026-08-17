from decimal import Decimal

from agents.tools.options_payoff import OptionLegInput, OptionsPayoffArgs, OptionsPayoffTool


def test_bull_call_spread_via_tool():
    tool = OptionsPayoffTool()
    args = OptionsPayoffArgs(
        strategy_label="bull call spread",
        legs=[
            OptionLegInput(
                option_type="call", action="buy", strike=Decimal(100), premium=Decimal(6)
            ),
            OptionLegInput(
                option_type="call", action="sell", strike=Decimal(110), premium=Decimal(2)
            ),
        ],
    )

    outcome = tool.run(args)

    assert outcome.success
    assert outcome.data["net_premium"] == "4"
    assert outcome.data["max_profit"] == "6"
    assert outcome.data["max_loss"] == "4"
    assert outcome.data["breakevens"] == ["104"]


def test_long_call_reports_unbounded_profit():
    tool = OptionsPayoffTool()
    args = OptionsPayoffArgs(
        strategy_label="long call",
        legs=[
            OptionLegInput(
                option_type="call", action="buy", strike=Decimal(100), premium=Decimal(5)
            )
        ],
    )

    outcome = tool.run(args)

    assert outcome.data["max_profit"] == "unbounded"
    assert outcome.data["max_loss"] == "5"
