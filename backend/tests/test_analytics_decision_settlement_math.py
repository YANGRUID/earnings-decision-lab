"""Phase 4.5 -- table-driven tests for analytics/decision/settlement_math.py:
realized P&L, return %, R-multiple, and win/loss, cross-checked against
independently hand-computed figures (matching this project's existing
practice for the Wilson-CI probability tests and the settlement/P&L math
testing note in PHASE4_ARCHITECTURE_REVIEW.md sec 8)."""

from decimal import Decimal

from analytics.decision.settlement_math import (
    SettlementLegInput,
    compute_settlement_totals,
    leg_exit_signed_value_per_ratio_share,
    leg_realized_pnl_per_share,
)
from models.enums import OptionAction


def test_long_leg_realized_pnl_is_exit_minus_entry():
    # Bought at ask 2.00, sold at bid 3.00 -- a $1.00/share gain.
    pnl = leg_realized_pnl_per_share(OptionAction.BUY, Decimal("2.00"), Decimal("3.00"))
    assert pnl == Decimal("1.00")


def test_long_leg_realized_pnl_is_negative_when_it_decayed():
    pnl = leg_realized_pnl_per_share(OptionAction.BUY, Decimal("2.00"), Decimal("0.50"))
    assert pnl == Decimal("-1.50")


def test_short_leg_realized_pnl_is_entry_minus_exit():
    # Sold at bid 3.00 to open (a credit), bought at ask 1.00 to close --
    # a $2.00/share gain (the position decayed, as a short position wants).
    pnl = leg_realized_pnl_per_share(OptionAction.SELL, Decimal("3.00"), Decimal("1.00"))
    assert pnl == Decimal("2.00")


def test_short_leg_realized_pnl_is_negative_when_it_appreciated():
    pnl = leg_realized_pnl_per_share(OptionAction.SELL, Decimal("3.00"), Decimal("5.00"))
    assert pnl == Decimal("-2.00")


def test_long_leg_exit_signed_value_is_a_credit():
    # Closing a long by selling at 3.00 is a credit -- negative, mirroring
    # signed_premium's "positive = debit, negative = credit" convention.
    value = leg_exit_signed_value_per_ratio_share(OptionAction.BUY, Decimal("3.00"), 1)
    assert value == Decimal("-3.00")


def test_short_leg_exit_signed_value_is_a_debit():
    # Closing a short by buying at 1.00 is a debit -- positive.
    value = leg_exit_signed_value_per_ratio_share(OptionAction.SELL, Decimal("1.00"), 1)
    assert value == Decimal("1.00")


def test_single_long_leg_totals_hand_computed():
    """1 contract, entry ask 2.00, exit bid 3.00, multiplier 100 --
    realized_pnl = 1.00 * 1 * 100 * 1 = 100.00. net_entry_cash 200.00
    (paid to open) -> return_pct = 100/200*100 = 50%. initial_max_risk
    200.00 -> r_multiple = 100/200 = 0.5."""
    legs = [
        SettlementLegInput(
            action=OptionAction.BUY,
            entry_price=Decimal("2.00"),
            exit_price=Decimal("3.00"),
            quantity=1,
            multiplier=Decimal("100"),
        )
    ]
    totals = compute_settlement_totals(
        legs, contracts=1, net_entry_cash=Decimal("200.00"), initial_max_risk=Decimal("200.00")
    )
    assert totals.realized_pnl == Decimal("100.00")
    assert totals.return_pct == Decimal("50.00")
    assert totals.r_multiple == Decimal("0.5")
    assert totals.is_win is True
    # Closing the long by selling at 3.00 is a credit (-3.00/share) --
    # net_exit_cash = -3.00 * 100 * 1 = -300.00.
    assert totals.net_exit_price_per_share == Decimal("-3.00")
    assert totals.net_exit_cash == Decimal("-300.00")


def test_losing_trade_hand_computed():
    """Same setup, but the option decayed: entry ask 2.00, exit bid 0.50
    -- realized_pnl = -1.50 * 1 * 100 * 1 = -150.00. return_pct =
    -150/200*100 = -75%. r_multiple = -150/200 = -0.75."""
    legs = [
        SettlementLegInput(
            action=OptionAction.BUY,
            entry_price=Decimal("2.00"),
            exit_price=Decimal("0.50"),
            quantity=1,
            multiplier=Decimal("100"),
        )
    ]
    totals = compute_settlement_totals(
        legs, contracts=1, net_entry_cash=Decimal("200.00"), initial_max_risk=Decimal("200.00")
    )
    assert totals.realized_pnl == Decimal("-150.00")
    assert totals.return_pct == Decimal("-75.00")
    assert totals.r_multiple == Decimal("-0.75")
    assert totals.is_win is False


def test_capped_max_loss_trade_r_multiple_is_exactly_minus_one():
    """A total loss (option expired/closed worthless: exit price 0) on a
    position whose entry cost equals its own max risk -- the defining
    "R multiple of exactly -1" case."""
    legs = [
        SettlementLegInput(
            action=OptionAction.BUY,
            entry_price=Decimal("2.00"),
            exit_price=Decimal("0.00"),
            quantity=1,
            multiplier=Decimal("100"),
        )
    ]
    totals = compute_settlement_totals(
        legs, contracts=1, net_entry_cash=Decimal("200.00"), initial_max_risk=Decimal("200.00")
    )
    assert totals.realized_pnl == Decimal("-200.00")
    assert totals.r_multiple == Decimal("-1")
    assert totals.is_win is False


def test_breakeven_trade_is_not_a_win():
    """realized_pnl of exactly zero -- is_win is strictly > 0, so a
    breakeven trade is never misreported as a win."""
    legs = [
        SettlementLegInput(
            action=OptionAction.BUY,
            entry_price=Decimal("2.00"),
            exit_price=Decimal("2.00"),
            quantity=1,
            multiplier=Decimal("100"),
        )
    ]
    totals = compute_settlement_totals(
        legs, contracts=1, net_entry_cash=Decimal("200.00"), initial_max_risk=Decimal("200.00")
    )
    assert totals.realized_pnl == Decimal("0")
    assert totals.is_win is False


def test_multi_leg_butterfly_totals_sum_each_leg_correctly():
    """1/2/1 butterfly (buy 95c, sell 2x 100c, buy 105c), 1 contract set,
    each leg's own real entry/exit -- realized_pnl is the real sum of
    every leg's own contribution, never a single blended number."""
    legs = [
        SettlementLegInput(  # buy 95c: entry ask 6.00, exit bid 8.00 -> +2.00/sh * 1 * 100 = +200
            action=OptionAction.BUY,
            entry_price=Decimal("6.00"),
            exit_price=Decimal("8.00"),
            quantity=1,
            multiplier=Decimal("100"),
        ),
        SettlementLegInput(  # sell 100c x2: entry bid 3.00, exit ask 3.50 -> -0.50/sh*2*100=-100
            action=OptionAction.SELL,
            entry_price=Decimal("3.00"),
            exit_price=Decimal("3.50"),
            quantity=2,
            multiplier=Decimal("100"),
        ),
        SettlementLegInput(  # buy 105c: entry ask 1.00, exit bid 0.50 -> -0.50/sh * 1 * 100 = -50
            action=OptionAction.BUY,
            entry_price=Decimal("1.00"),
            exit_price=Decimal("0.50"),
            quantity=1,
            multiplier=Decimal("100"),
        ),
    ]
    totals = compute_settlement_totals(
        legs, contracts=1, net_entry_cash=Decimal("100.00"), initial_max_risk=Decimal("400.00")
    )
    # 200 - 100 - 50 = 50
    assert totals.realized_pnl == Decimal("50.00")
    assert totals.return_pct == Decimal("50.00")  # 50/100*100
    assert totals.r_multiple == Decimal("0.125")  # 50/400
    assert totals.is_win is True


def test_contracts_scales_every_leg_uniformly():
    """The same single-leg trade, bought as 3 contract sets instead of 1
    -- realized_pnl scales linearly with contracts, matching how
    net_entry_cash itself already scales (analytics/decision/budget.py's
    total_net_premium = net_premium * CONTRACT_MULTIPLIER * quantity)."""
    legs = [
        SettlementLegInput(
            action=OptionAction.BUY,
            entry_price=Decimal("2.00"),
            exit_price=Decimal("3.00"),
            quantity=1,
            multiplier=Decimal("100"),
        )
    ]
    totals = compute_settlement_totals(
        legs, contracts=3, net_entry_cash=Decimal("600.00"), initial_max_risk=Decimal("600.00")
    )
    assert totals.realized_pnl == Decimal("300.00")  # 1.00 * 1 * 100 * 3
    assert totals.return_pct == Decimal("50.00")
    assert totals.r_multiple == Decimal("0.5")


def test_return_pct_is_none_when_net_entry_cash_is_zero():
    """Division by zero is never silently coerced to 0 or infinity --
    an undefined return_pct stays honestly None."""
    legs = [
        SettlementLegInput(
            action=OptionAction.BUY,
            entry_price=Decimal("2.00"),
            exit_price=Decimal("3.00"),
            quantity=1,
            multiplier=Decimal("100"),
        )
    ]
    totals = compute_settlement_totals(
        legs, contracts=1, net_entry_cash=Decimal("0"), initial_max_risk=Decimal("200.00")
    )
    assert totals.return_pct is None
    assert totals.r_multiple is not None  # unaffected by the other denominator


def test_r_multiple_is_none_when_initial_max_risk_is_unavailable():
    legs = [
        SettlementLegInput(
            action=OptionAction.BUY,
            entry_price=Decimal("2.00"),
            exit_price=Decimal("3.00"),
            quantity=1,
            multiplier=Decimal("100"),
        )
    ]
    totals = compute_settlement_totals(
        legs, contracts=1, net_entry_cash=Decimal("200.00"), initial_max_risk=None
    )
    assert totals.r_multiple is None
    assert totals.return_pct is not None  # unaffected by the other denominator


def test_return_pct_reflects_negative_net_entry_cash_for_a_credit_strategy():
    """Phase 4.5 approved decision 2, taken literally: return_pct =
    realized_pnl / net_entry_cash exactly as specified, even though a
    net-credit strategy makes net_entry_cash negative (payoff.py's own
    documented sign convention) -- never silently reinterpreted or
    clamped to a positive denominator."""
    legs = [
        SettlementLegInput(  # a short leg that decayed favorably
            action=OptionAction.SELL,
            entry_price=Decimal("3.00"),
            exit_price=Decimal("1.00"),
            quantity=1,
            multiplier=Decimal("100"),
        )
    ]
    # A net credit of -300 was received to open (sold for 3.00 -> -300
    # in the signed_premium convention).
    totals = compute_settlement_totals(
        legs, contracts=1, net_entry_cash=Decimal("-300.00"), initial_max_risk=Decimal("500.00")
    )
    assert totals.realized_pnl == Decimal("200.00")  # 2.00/sh * 1 * 100
    assert totals.return_pct == Decimal("200.00") / Decimal("-300.00") * 100
    assert totals.return_pct < 0  # the literal formula's real, expected behavior here
