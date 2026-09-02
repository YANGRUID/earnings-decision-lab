"""V4.4A T+1 valuation context tests (2026-09-03). Verifies the entry-
executable-price rule (Section 5: BUY->ASK, SELL->BID, never midpoint)
and the DTE/holding-period accessors (Section 2)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_expected_move import derive_expected_move_context
from analytics.decision.v4_t1_valuation_context import (
    MIN_DTE_FOR_PRICING,
    V4T1LegInput,
    V4T1ValuationContext,
)
from analytics.options.strategy_candidates import StrategyCategory

EXP = date(2026, 9, 18)
NOW = datetime(2026, 9, 1, tzinfo=UTC)
EXIT_TS = datetime(2026, 9, 2, tzinfo=UTC)


def _leg(
    action: str, bid: Decimal | None = Decimal("4.90"), ask: Decimal | None = Decimal("5.10")
) -> V4T1LegInput:
    return V4T1LegInput(
        leg_index=0,
        action=action,  # type: ignore[arg-type]
        right="call",
        strike=Decimal("100"),
        quantity=1,
        multiplier=Decimal("100"),
        entry_bid=bid,
        entry_ask=ask,
        entry_last=None,
        entry_iv=Decimal("0.5"),
        entry_delta=None,
        entry_gamma=None,
        entry_theta=None,
        entry_vega=None,
        market_data_quality="live",
        external_contract_id=None,
    )


def _bare_context() -> object:
    return derive_expected_move_context(
        spot=Decimal("100"),
        observed_at=NOW,
        expiration=None,
        quotes_for_expiration=None,
        historical_next_day_move_pcts=None,
    )


class TestEntryExecutablePrice:
    def test_buy_uses_ask(self):
        leg = _leg("buy")
        assert leg.entry_executable_price == Decimal("5.10")

    def test_sell_uses_bid(self):
        leg = _leg("sell")
        assert leg.entry_executable_price == Decimal("4.90")

    def test_never_the_midpoint(self):
        leg = _leg("buy")
        assert leg.entry_executable_price != leg.entry_mid_price

    def test_mid_price_available_informationally(self):
        leg = _leg("buy")
        assert leg.entry_mid_price == Decimal("5.00")

    def test_none_when_relevant_side_missing(self):
        leg = _leg("buy", ask=None)
        assert leg.entry_executable_price is None

    def test_mid_none_when_either_side_missing(self):
        leg = _leg("buy", ask=None)
        assert leg.entry_mid_price is None


class TestDteAndHoldingPeriod:
    def _context(self, expiration=EXP) -> V4T1ValuationContext:
        return V4T1ValuationContext(
            ticker="ZZ",
            underlying_price=Decimal("100"),
            observed_at=NOW,
            entry_timestamp=NOW,
            expected_exit_timestamp=EXIT_TS,
            strategy=StrategyCategory.LONG_CALL,
            expiration=expiration,
            legs=(_leg("buy"),),
            expected_move_context=_bare_context(),
        )

    def test_holding_period_matches_entry_to_exit(self):
        ctx = self._context()
        assert ctx.holding_period == EXIT_TS - NOW

    def test_dte_entry_and_exit_real_values(self):
        ctx = self._context()
        assert ctx.dte_entry == (EXP - NOW.date()).days
        assert ctx.dte_exit == (EXP - EXIT_TS.date()).days

    def test_dte_exit_for_pricing_floors_at_minimum(self):
        """Section 2's own concern: never accidentally value at
        expiration -- a same-day-as-expiration exit still gets a
        strictly positive DTE for Black-Scholes."""
        ctx = self._context(expiration=EXIT_TS.date())
        assert ctx.dte_exit == 0
        assert ctx.dte_exit_for_pricing() == MIN_DTE_FOR_PRICING

    def test_dte_exit_for_pricing_never_floors_a_genuinely_larger_value(self):
        ctx = self._context()
        assert ctx.dte_exit_for_pricing() == ctx.dte_exit
        assert ctx.dte_exit_for_pricing() > MIN_DTE_FOR_PRICING
