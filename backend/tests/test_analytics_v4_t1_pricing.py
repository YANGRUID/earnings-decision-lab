"""V4.4A T+1 option repricing & scenario valuation tests (2026-09-03).

Covers the mandatory Section 43 list: option pricing sanity, near-zero-
time behavior, IV sensitivity, underlying-direction sensitivity,
butterfly/straddle/IV-crush/directional-spread behavior (Sections
22-26), execution friction (long exit BID, short exit ASK), the early-
exit max-loss test (Section 27), standardized-capital return,
delayed-data labeling, no-lookahead, and deterministic repeatability.

Every mandatory economic-behavior test (butterfly, straddle, condor,
directional) uses Black-Scholes-CONSISTENT entry quotes (computed via
the real ``price_and_greeks`` kernel at a real entry IV/DTE, not an
arbitrary synthetic formula) -- otherwise entry cost and T+1 model
value would be on different, incomparable economic bases, producing
meaningless P&L. This was confirmed the hard way: an earlier hand-run
against synthetic (non-BS) entry quotes produced a butterfly that
"profited" in every single scenario, including the largest moves --
economically backwards, and purely an artifact of inconsistent entry
pricing, not a flaw in the engine. Every test below was hand-verified
against a manual script run before being written here; none were
fitted to any of the 7 real settled V3 losses (Section 35)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_expected_move import derive_expected_move_context
from analytics.decision.v4_t1_pricing import (
    T1_RISK_FREE_RATE_ASSUMPTION,
    evaluate_candidate_t1_scenario,
    evaluate_candidate_t1_scenarios,
    price_leg_at_scenario,
    summarize_candidate_distribution,
)
from analytics.decision.v4_t1_scenario_grid import build_iv_scenarios, build_underlying_scenarios
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from analytics.options.black_scholes import price_and_greeks
from analytics.options.strategy_candidates import StrategyCategory
from models.enums import OptionType
from providers.types import OptionQuote

EXP = date(2026, 9, 4)  # short-dated, close to entry -- realistic earnings weekly
ENTRY_DATE = date(2026, 9, 1)
NOW = datetime(2026, 9, 1, tzinfo=UTC)
EXIT_TS = datetime(2026, 9, 2, tzinfo=UTC)
SPOT = Decimal("100")
ENTRY_IV = Decimal("0.60")


def _bs_entry_price(strike: Decimal, right: str, dte: int | None = None) -> Decimal:
    dte_entry = dte if dte is not None else (EXP - ENTRY_DATE).days
    tte = Decimal(dte_entry) / Decimal(365)
    greeks = price_and_greeks(
        OptionType(right),
        float(SPOT),
        float(strike),
        float(tte),
        float(T1_RISK_FREE_RATE_ASSUMPTION),
        float(ENTRY_IV),
    )
    return Decimal(str(greeks.price))


def _quote(strike: Decimal, right: str) -> OptionQuote:
    mid = _bs_entry_price(strike, right)
    spread = mid * Decimal("0.10")
    return OptionQuote(
        ticker="ZZ",
        snapshot_timestamp=NOW,
        expiration_date=EXP,
        strike=strike,
        option_type=right,
        bid=max(mid - spread / 2, Decimal("0.01")),
        ask=mid + spread / 2,
        source_provider="test",
        retrieved_at=NOW,
    )


def _leg(
    leg_index: int,
    action: str,
    right: str,
    strike: Decimal,
    qty: int = 1,
    market_data_quality: str | None = "live",
) -> V4T1LegInput:
    q = _quote(strike, right)
    return V4T1LegInput(
        leg_index=leg_index,
        action=action,  # type: ignore[arg-type]
        right=right,  # type: ignore[arg-type]
        strike=strike,
        quantity=qty,
        multiplier=Decimal("100"),
        entry_bid=q.bid,
        entry_ask=q.ask,
        entry_last=None,
        entry_iv=ENTRY_IV,
        entry_delta=None,
        entry_gamma=None,
        entry_theta=None,
        entry_vega=None,
        market_data_quality=market_data_quality,
        external_contract_id=None,
    )


def _wide_context():
    strikes = [SPOT + Decimal(5) * i for i in range(-4, 5)]
    quotes = [_quote(s, r) for s in strikes for r in ("call", "put")]
    return derive_expected_move_context(
        spot=SPOT,
        observed_at=NOW,
        expiration=EXP,
        quotes_for_expiration=quotes,
        historical_next_day_move_pcts=[Decimal("0.05")] * 6,
    )


def _context(strategy, legs) -> V4T1ValuationContext:
    return V4T1ValuationContext(
        ticker="ZZ",
        underlying_price=SPOT,
        observed_at=NOW,
        entry_timestamp=NOW,
        expected_exit_timestamp=EXIT_TS,
        strategy=strategy,
        expiration=EXP,
        legs=legs,
        expected_move_context=_wide_context(),
    )


class TestOptionPricingSanity:
    def test_call_price_increases_with_spot(self):
        iv = build_iv_scenarios()[1]
        leg = _leg(0, "buy", "call", SPOT)
        low = price_leg_at_scenario(leg, Decimal("95"), iv, 2, "NORMAL_FRICTION")
        high = price_leg_at_scenario(leg, Decimal("105"), iv, 2, "NORMAL_FRICTION")
        assert low.model_price is not None
        assert high.model_price is not None
        assert high.model_price > low.model_price

    def test_put_price_decreases_with_spot(self):
        iv = build_iv_scenarios()[1]
        leg = _leg(0, "buy", "put", SPOT)
        low = price_leg_at_scenario(leg, Decimal("95"), iv, 2, "NORMAL_FRICTION")
        high = price_leg_at_scenario(leg, Decimal("105"), iv, 2, "NORMAL_FRICTION")
        assert low.model_price is not None
        assert high.model_price is not None
        assert low.model_price > high.model_price

    def test_model_price_never_negative(self):
        iv = build_iv_scenarios()[0]
        leg = _leg(0, "buy", "put", SPOT)
        result = price_leg_at_scenario(leg, Decimal("500"), iv, 2, "NORMAL_FRICTION")
        assert result.model_price is not None
        assert result.model_price >= 0


class TestNearZeroTimeBehavior:
    def test_dte_of_one_does_not_crash(self):
        iv = build_iv_scenarios()[1]
        leg = _leg(0, "buy", "call", SPOT)
        result = price_leg_at_scenario(leg, SPOT, iv, 1, "NORMAL_FRICTION")
        assert result.model_price is not None

    def test_near_expiration_atm_price_shrinks_toward_intrinsic(self):
        iv = build_iv_scenarios()[1]
        leg = _leg(0, "buy", "call", SPOT)
        far = price_leg_at_scenario(leg, SPOT, iv, 30, "NORMAL_FRICTION")
        near = price_leg_at_scenario(leg, SPOT, iv, 1, "NORMAL_FRICTION")
        assert far.model_price is not None
        assert near.model_price is not None
        assert near.model_price < far.model_price  # less extrinsic value remains


class TestIVSensitivity:
    def test_higher_iv_scenario_increases_long_option_value(self):
        leg = _leg(0, "buy", "call", SPOT)
        scenarios = {s.label: s for s in build_iv_scenarios()}
        weak = price_leg_at_scenario(
            leg, SPOT, scenarios["WEAK_CRUSH_OR_ELEVATED"], 2, "NORMAL_FRICTION"
        )
        strong = price_leg_at_scenario(leg, SPOT, scenarios["STRONG_CRUSH"], 2, "NORMAL_FRICTION")
        assert weak.model_price is not None
        assert strong.model_price is not None
        assert weak.model_price > strong.model_price

    def test_no_entry_iv_is_honestly_unpriced(self):
        leg = _leg(0, "buy", "call", SPOT)
        leg_no_iv = V4T1LegInput(**{**leg.__dict__, "entry_iv": None})
        iv = build_iv_scenarios()[0]
        result = price_leg_at_scenario(leg_no_iv, SPOT, iv, 2, "NORMAL_FRICTION")
        assert result.model_price is None
        assert "NO_ENTRY_IV" in result.reason_codes


class TestButterflyLargeMoveBehavior:
    """Mandatory Section 22 test: expiration payoff can look attractive
    near center, but under large-move T+1 scenarios the candidate
    loses -- emerging from real Black-Scholes mechanics, never a
    hard-coded penalty."""

    def _butterfly_context(self):
        legs = (
            _leg(0, "buy", "call", Decimal("95")),
            _leg(1, "sell", "call", Decimal("100"), qty=2),
            _leg(2, "buy", "call", Decimal("105")),
        )
        return _context(StrategyCategory.LONG_CALL_BUTTERFLY, legs)

    def test_losses_grow_as_underlying_moves_away_from_center(self):
        ctx = self._butterfly_context()
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        normal_crush = {
            r.underlying_move_label: r for r in results if r.iv_scenario_label == "NORMAL_CRUSH"
        }
        flat_pnl = normal_crush["FLAT"].realized_equivalent_pnl_executable
        large_up_pnl = normal_crush["LARGE_UPSIDE"].realized_equivalent_pnl_executable
        large_down_pnl = normal_crush["LARGE_DOWNSIDE"].realized_equivalent_pnl_executable
        assert flat_pnl is not None and large_up_pnl is not None and large_down_pnl is not None
        assert flat_pnl > large_up_pnl
        assert flat_pnl > large_down_pnl

    def test_large_move_scenario_is_a_real_loss(self):
        """The specific, mandatory claim: under a genuinely large move,
        the candidate LOSES money, not merely "profits less"."""
        ctx = self._butterfly_context()
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        large_up = next(
            r
            for r in results
            if r.underlying_move_label == "LARGE_UPSIDE" and r.iv_scenario_label == "NORMAL_CRUSH"
        )
        assert large_up.realized_equivalent_pnl_executable is not None
        assert large_up.realized_equivalent_pnl_executable < 0

    def test_this_emerges_from_pricing_not_a_hardcoded_rule(self):
        """No special-casing exists for LONG_CALL_BUTTERFLY anywhere in
        the pricing module -- confirmed by grepping the real source."""
        import inspect

        import analytics.decision.v4_t1_pricing as pricing_module

        source = inspect.getsource(pricing_module)
        assert "LONG_CALL_BUTTERFLY" not in source
        assert "StrategyCategory.LONG_CALL_BUTTERFLY" not in source


class TestStraddleLargeMoveBehavior:
    """Mandatory Section 23 test: two-sided convex structures benefit
    mechanically under large moves; under small move + strong crush
    they suffer."""

    def _straddle_context(self):
        legs = (_leg(0, "buy", "call", SPOT), _leg(1, "buy", "put", SPOT))
        return _context(StrategyCategory.LONG_STRADDLE, legs)

    def test_large_move_with_elevated_iv_is_the_best_outcome(self):
        ctx = self._straddle_context()
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        by_id = {r.scenario_id: r for r in results}
        best = by_id["LARGE_UPSIDE__WEAK_CRUSH_OR_ELEVATED"]
        worst = by_id["FLAT__STRONG_CRUSH"]
        assert best.realized_equivalent_pnl_executable is not None
        assert worst.realized_equivalent_pnl_executable is not None
        assert best.realized_equivalent_pnl_executable > worst.realized_equivalent_pnl_executable

    def test_flat_plus_strong_crush_is_a_real_loss(self):
        ctx = self._straddle_context()
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        flat_crush = next(
            r
            for r in results
            if r.underlying_move_label == "FLAT" and r.iv_scenario_label == "STRONG_CRUSH"
        )
        assert flat_crush.realized_equivalent_pnl_executable is not None
        assert flat_crush.realized_equivalent_pnl_executable < 0

    def test_large_move_improves_outcome_at_fixed_iv_scenario(self):
        """Holding IV scenario fixed, moving from FLAT to LARGE_UPSIDE
        should mechanically improve a straddle's P&L (Section 23)."""
        ctx = self._straddle_context()
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        for iv_label in ("STRONG_CRUSH", "NORMAL_CRUSH", "WEAK_CRUSH_OR_ELEVATED"):
            flat = next(
                r
                for r in results
                if r.underlying_move_label == "FLAT" and r.iv_scenario_label == iv_label
            )
            large_up = next(
                r
                for r in results
                if r.underlying_move_label == "LARGE_UPSIDE" and r.iv_scenario_label == iv_label
            )
            assert flat.realized_equivalent_pnl_executable is not None
            assert large_up.realized_equivalent_pnl_executable is not None
            assert (
                large_up.realized_equivalent_pnl_executable
                > flat.realized_equivalent_pnl_executable
            )


class TestShortVolRangeBehavior:
    """Mandatory Section 24 test: iron condor should show real
    scenario dependence -- profits should shrink moving from the
    center toward the wings."""

    def test_condor_profit_declines_toward_the_wings(self):
        legs = (
            _leg(0, "buy", "put", Decimal("85")),
            _leg(1, "sell", "put", Decimal("90")),
            _leg(2, "sell", "call", Decimal("110")),
            _leg(3, "buy", "call", Decimal("115")),
        )
        ctx = _context(StrategyCategory.IRON_CONDOR, legs)
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        normal = {
            r.underlying_move_label: r for r in results if r.iv_scenario_label == "NORMAL_CRUSH"
        }
        flat_pnl = normal["FLAT"].realized_equivalent_pnl_executable
        large_up_pnl = normal["LARGE_UPSIDE"].realized_equivalent_pnl_executable
        large_down_pnl = normal["LARGE_DOWNSIDE"].realized_equivalent_pnl_executable
        assert flat_pnl is not None and large_up_pnl is not None and large_down_pnl is not None
        assert flat_pnl > large_up_pnl
        assert flat_pnl > large_down_pnl


class TestDirectionalSpreadBehavior:
    """Mandatory Section 25 test: bull call / long call favors upside;
    bear structures mirror. Must come from pricing, not a semantic
    score (v4_compatibility.py is never imported here)."""

    def test_long_call_upside_beats_downside(self):
        legs = (_leg(0, "buy", "call", SPOT),)
        ctx = _context(StrategyCategory.LONG_CALL, legs)
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        normal = {
            r.underlying_move_label: r for r in results if r.iv_scenario_label == "NORMAL_CRUSH"
        }
        assert normal["LARGE_UPSIDE"].realized_equivalent_pnl_executable is not None
        assert normal["LARGE_DOWNSIDE"].realized_equivalent_pnl_executable is not None
        assert (
            normal["LARGE_UPSIDE"].realized_equivalent_pnl_executable
            > normal["LARGE_DOWNSIDE"].realized_equivalent_pnl_executable
        )

    def test_monotonic_in_underlying_move_direction(self):
        legs = (_leg(0, "buy", "call", SPOT),)
        ctx = _context(StrategyCategory.LONG_CALL, legs)
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        order = [
            "LARGE_DOWNSIDE",
            "MODERATE_DOWNSIDE",
            "SMALL_DOWNSIDE",
            "FLAT",
            "SMALL_UPSIDE",
            "MODERATE_UPSIDE",
            "LARGE_UPSIDE",
        ]
        normal = {
            r.underlying_move_label: r for r in results if r.iv_scenario_label == "NORMAL_CRUSH"
        }
        pnls = [normal[label].realized_equivalent_pnl_executable for label in order]
        assert all(
            a is not None and b is not None and b > a for a, b in zip(pnls, pnls[1:], strict=False)
        )

    def test_no_v4_2_semantics_imported_into_pricing(self):
        import inspect

        import analytics.decision.v4_t1_pricing as pricing_module

        source = inspect.getsource(pricing_module)
        assert "v4_compatibility" not in source
        assert "SemanticCompatibility" not in source


class TestTimeValueDistinctFromExpirationPayoff:
    """Mandatory Section 26 regression: two candidates with identical
    expiration payoff shape may have materially different T+1 values
    due to remaining time/IV/moneyness -- proves V4.4A does NOT use an
    expiration intrinsic-value shortcut."""

    def test_same_intrinsic_value_different_t1_model_price_by_iv(self):
        # Deep ITM call: at expiration its payoff is pure intrinsic
        # regardless of IV, but at T+1 (2 real days before expiry) its
        # THEORETICAL value still carries real extrinsic value that
        # differs by IV scenario.
        leg = _leg(0, "buy", "call", Decimal("80"))  # deep ITM at spot=100
        scenarios = {s.label: s for s in build_iv_scenarios()}
        low_iv = price_leg_at_scenario(leg, SPOT, scenarios["STRONG_CRUSH"], 2, "NORMAL_FRICTION")
        high_iv = price_leg_at_scenario(
            leg, SPOT, scenarios["WEAK_CRUSH_OR_ELEVATED"], 2, "NORMAL_FRICTION"
        )
        assert low_iv.model_price is not None
        assert high_iv.model_price is not None
        intrinsic = SPOT - Decimal("80")
        assert low_iv.model_price > intrinsic  # real extrinsic value remains
        assert high_iv.model_price > low_iv.model_price  # and it differs by IV


class TestExecutionFriction:
    def test_long_close_uses_bid_side_discount(self):
        iv = build_iv_scenarios()[1]
        leg = _leg(0, "buy", "call", SPOT)
        result = price_leg_at_scenario(leg, SPOT, iv, 2, "NORMAL_FRICTION")
        assert result.model_price is not None
        assert result.executable_exit_price is not None
        assert result.executable_exit_price < result.model_price

    def test_short_close_uses_ask_side_premium(self):
        iv = build_iv_scenarios()[1]
        leg = _leg(0, "sell", "call", SPOT)
        result = price_leg_at_scenario(leg, SPOT, iv, 2, "NORMAL_FRICTION")
        assert result.model_price is not None
        assert result.executable_exit_price is not None
        assert result.executable_exit_price > result.model_price

    def test_higher_friction_widens_the_gap(self):
        iv = build_iv_scenarios()[1]
        leg = _leg(0, "buy", "call", SPOT)
        low = price_leg_at_scenario(leg, SPOT, iv, 2, "LOW_FRICTION")
        high = price_leg_at_scenario(leg, SPOT, iv, 2, "HIGH_FRICTION")
        assert low.model_price is not None and high.model_price is not None
        assert low.executable_exit_price is not None and high.executable_exit_price is not None
        low_gap = low.model_price - low.executable_exit_price
        high_gap = high.model_price - high.executable_exit_price
        assert high_gap > low_gap


class TestEarlyExitMaxLossExceedsTheoretical:
    """Mandatory Section 27 test: a defined-risk expiration structure
    (credit spread) can produce a modeled EXECUTABLE T+1 loss larger
    than its theoretical expiration max-loss, because closing BOTH
    legs pays real friction on each side (buy back the short at the
    ask-side premium, sell the long protective leg at the bid-side
    discount) -- the conceptual DY issue, reproduced from mechanics,
    without touching DY's own historical record.

    Needs the position genuinely close to expiration-like intrinsic
    convergence (DTE_exit at the pricing floor) AND a width narrow
    enough that the real scenario grid's own implied-move boundary
    carries the underlying past both strikes -- otherwise real
    remaining time value legitimately cushions the loss below the
    theoretical max, which is itself an honest, correct model
    behavior, just not the specific mechanism this test demonstrates.
    Confirmed by hand-run before being written here (see the V4.4A
    report's own Section K)."""

    def _near_expiry_context(self, width: Decimal) -> V4T1ValuationContext:
        expiration = date(2026, 9, 3)  # exit is Sept 2 -> DTE_exit floors at 1
        strikes = [SPOT + Decimal("1") * i for i in range(-20, 21)]

        def q(strike: Decimal, right: str) -> OptionQuote:
            dte_entry = (expiration - ENTRY_DATE).days
            mid = _bs_entry_price(strike, right, dte=dte_entry)
            spread = mid * Decimal("0.10")
            return OptionQuote(
                ticker="ZZ",
                snapshot_timestamp=NOW,
                expiration_date=expiration,
                strike=strike,
                option_type=right,
                bid=max(mid - spread / 2, Decimal("0.01")),
                ask=mid + spread / 2,
                source_provider="test",
                retrieved_at=NOW,
            )

        def leg(index: int, action: str, right: str, strike: Decimal) -> V4T1LegInput:
            quote = q(strike, right)
            return V4T1LegInput(
                leg_index=index,
                action=action,  # type: ignore[arg-type]
                right=right,  # type: ignore[arg-type]
                strike=strike,
                quantity=1,
                multiplier=Decimal("100"),
                entry_bid=quote.bid,
                entry_ask=quote.ask,
                entry_last=None,
                entry_iv=ENTRY_IV,
                entry_delta=None,
                entry_gamma=None,
                entry_theta=None,
                entry_vega=None,
                market_data_quality="live",
                external_contract_id=None,
            )

        legs = (leg(0, "sell", "call", SPOT), leg(1, "buy", "call", SPOT + width))
        quotes = [q(s, r) for s in strikes for r in ("call", "put")]
        expected_move_context = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=expiration,
            quotes_for_expiration=quotes,
            historical_next_day_move_pcts=[Decimal("0.05")] * 6,
        )
        return V4T1ValuationContext(
            ticker="ZZ",
            underlying_price=SPOT,
            observed_at=NOW,
            entry_timestamp=NOW,
            expected_exit_timestamp=EXIT_TS,
            strategy=StrategyCategory.CALL_CREDIT_SPREAD,
            expiration=expiration,
            legs=legs,
            expected_move_context=expected_move_context,
        )

    def test_executable_loss_can_exceed_theoretical_max_loss(self):
        width = Decimal("2")
        ctx = self._near_expiry_context(width)
        entry_cf = sum(
            (-1 if leg.action == "buy" else 1)
            * (leg.entry_executable_price or Decimal(0))
            * leg.quantity
            * leg.multiplier
            for leg in ctx.legs
        )
        theoretical_max_loss = width * Decimal("100") - entry_cf  # credit received reduces max loss
        assert theoretical_max_loss > 0

        results = evaluate_candidate_t1_scenarios(ctx, "test", friction_level="HIGH_FRICTION")
        assert results is not None
        worst = min(
            (r for r in results if r.realized_equivalent_pnl_executable is not None),
            key=lambda r: r.realized_equivalent_pnl_executable or Decimal(0),
        )
        assert worst.realized_equivalent_pnl_executable is not None
        worst_loss = -worst.realized_equivalent_pnl_executable
        # The real, mandatory claim: friction can push the realized
        # executable loss beyond the theoretical expiration max-loss
        # denominator -- not asserting it always does at every
        # scenario, but that the HIGH_FRICTION worst case does here.
        assert worst_loss > theoretical_max_loss

    def test_low_friction_does_not_necessarily_exceed_it(self):
        """Contrast case: the SAME position under LOW_FRICTION should
        not (and in this configuration does not) exceed the
        theoretical max loss -- proving the effect above is genuinely
        driven by execution friction, not a modeling artifact that
        fires unconditionally."""
        width = Decimal("2")
        ctx = self._near_expiry_context(width)
        entry_cf = sum(
            (-1 if leg.action == "buy" else 1)
            * (leg.entry_executable_price or Decimal(0))
            * leg.quantity
            * leg.multiplier
            for leg in ctx.legs
        )
        theoretical_max_loss = width * Decimal("100") - entry_cf
        results = evaluate_candidate_t1_scenarios(ctx, "test", friction_level="LOW_FRICTION")
        assert results is not None
        worst = min(
            (r for r in results if r.realized_equivalent_pnl_executable is not None),
            key=lambda r: r.realized_equivalent_pnl_executable or Decimal(0),
        )
        assert worst.realized_equivalent_pnl_executable is not None
        worst_loss = -worst.realized_equivalent_pnl_executable
        assert worst_loss < theoretical_max_loss


class TestStandardizedCapitalReturn:
    def test_return_on_standardized_capital_is_pnl_over_2000(self):
        legs = (_leg(0, "buy", "call", SPOT),)
        ctx = _context(StrategyCategory.LONG_CALL, legs)
        iv = build_iv_scenarios()[1]
        underlying = build_underlying_scenarios(ctx.expected_move_context)
        assert underlying is not None
        result = evaluate_candidate_t1_scenario(ctx, underlying[0], iv, "NORMAL_FRICTION", "test")
        assert result.realized_equivalent_pnl_executable is not None
        assert result.return_on_standardized_capital_executable is not None
        assert (
            result.return_on_standardized_capital_executable
            == result.realized_equivalent_pnl_executable / Decimal(2000)
        )


class TestDelayedDataLabeling:
    def test_market_data_quality_carried_through_to_leg_value(self):
        iv = build_iv_scenarios()[1]
        leg = _leg(0, "buy", "call", SPOT, market_data_quality="delayed")
        result = price_leg_at_scenario(leg, SPOT, iv, 2, "NORMAL_FRICTION")
        assert result.entry_market_data_quality == "delayed"

    def test_none_quality_stays_none_not_fabricated(self):
        iv = build_iv_scenarios()[1]
        leg = _leg(0, "buy", "call", SPOT, market_data_quality=None)
        result = price_leg_at_scenario(leg, SPOT, iv, 2, "NORMAL_FRICTION")
        assert result.entry_market_data_quality is None


class TestNoLookahead:
    def test_context_carries_no_realized_outcome_field(self):
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(V4T1ValuationContext)}
        forbidden = {"realized_move", "pnl", "settlement", "exit_price", "outcome", "realized_pnl"}
        assert field_names.isdisjoint(forbidden)

    def test_leg_input_carries_no_realized_outcome_field(self):
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(V4T1LegInput)}
        forbidden = {"realized_move", "pnl", "settlement", "exit_price", "outcome", "realized_pnl"}
        assert field_names.isdisjoint(forbidden)


class TestDeterministicRepeatability:
    def test_identical_context_same_scenario_identical_result(self):
        legs = (_leg(0, "buy", "call", SPOT),)
        ctx = _context(StrategyCategory.LONG_CALL, legs)
        first = evaluate_candidate_t1_scenarios(ctx, "test")
        second = evaluate_candidate_t1_scenarios(ctx, "test")
        assert first == second

    def test_repeated_five_times_identical(self):
        legs = (_leg(0, "sell", "put", Decimal("95")), _leg(1, "buy", "put", Decimal("90")))
        ctx = _context(StrategyCategory.PUT_CREDIT_SPREAD, legs)
        runs = [evaluate_candidate_t1_scenarios(ctx, "test") for _ in range(5)]
        assert all(run == runs[0] for run in runs)


class TestDistributionSummaryTerminology:
    def test_scenario_average_never_called_expected_return(self):
        legs = (_leg(0, "buy", "call", SPOT),)
        ctx = _context(StrategyCategory.LONG_CALL, legs)
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        summary = summarize_candidate_distribution(results)
        assert summary.weighted_expected_return is None  # no legitimate weights supplied
        assert (
            "expected return" not in summary.quality_note.lower()
            or "never called" in summary.quality_note.lower()
        )
        assert summary.scenario_average_return is not None

    def test_weighted_expected_return_only_when_weights_supplied(self):
        legs = (_leg(0, "buy", "call", SPOT),)
        ctx = _context(StrategyCategory.LONG_CALL, legs)
        results = evaluate_candidate_t1_scenarios(ctx, "test")
        assert results is not None
        weights = {r.scenario_id: Decimal("1") for r in results}
        summary = summarize_candidate_distribution(results, scenario_weights=weights)
        assert summary.weighted_expected_return is not None
