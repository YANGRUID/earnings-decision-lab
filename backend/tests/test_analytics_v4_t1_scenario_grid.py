"""V4.4A T+1 scenario grid tests (2026-09-03). Verifies the
underlying-move grid (Section 7, EM-unit based, honest absence when no
evidence exists) and the IV-crush grid (Sections 9-11, heuristic/
uncalibrated, per-leg entry-IV preserving, never blended)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_expected_move import derive_expected_move_context
from analytics.decision.v4_t1_scenario_grid import (
    IV_CRUSH_SCENARIO_GRID,
    build_iv_scenarios,
    build_underlying_scenarios,
    scenario_leg_iv,
    summarize_iv_crush_diagnostic,
)
from providers.types import OptionQuote

EXP = date(2026, 9, 18)
NOW = datetime(2026, 8, 17, tzinfo=UTC)
SPOT = Decimal("100")


def _quote(strike: Decimal, option_type: str) -> OptionQuote:
    return OptionQuote(
        ticker="ZZ",
        snapshot_timestamp=NOW,
        expiration_date=EXP,
        strike=strike,
        option_type=option_type,
        bid=Decimal("4.90"),
        ask=Decimal("5.10"),
        source_provider="test",
        retrieved_at=NOW,
    )


def _context_with_implied_move():
    quotes = [_quote(SPOT, "call"), _quote(SPOT, "put")]
    return derive_expected_move_context(
        spot=SPOT,
        observed_at=NOW,
        expiration=EXP,
        quotes_for_expiration=quotes,
        historical_next_day_move_pcts=None,
    )


def _bare_context():
    return derive_expected_move_context(
        spot=SPOT,
        observed_at=NOW,
        expiration=None,
        quotes_for_expiration=None,
        historical_next_day_move_pcts=None,
    )


class TestUnderlyingScenarios:
    def test_seven_named_scenarios(self):
        scenarios = build_underlying_scenarios(_context_with_implied_move())
        assert scenarios is not None
        assert len(scenarios) == 7
        labels = [s.label for s in scenarios]
        assert labels == [
            "LARGE_DOWNSIDE",
            "MODERATE_DOWNSIDE",
            "SMALL_DOWNSIDE",
            "FLAT",
            "SMALL_UPSIDE",
            "MODERATE_UPSIDE",
            "LARGE_UPSIDE",
        ]

    def test_flat_is_exactly_spot(self):
        scenarios = build_underlying_scenarios(_context_with_implied_move())
        flat = next(s for s in scenarios if s.label == "FLAT")
        assert flat.scenario_underlying_price == SPOT
        assert flat.move_dollars == Decimal(0)

    def test_large_upside_is_full_implied_move(self):
        ctx = _context_with_implied_move()
        scenarios = build_underlying_scenarios(ctx)
        large_up = next(s for s in scenarios if s.label == "LARGE_UPSIDE")
        assert large_up.scenario_underlying_price == SPOT + ctx.implied_move_dollars
        assert large_up.em_fraction == Decimal("1")

    def test_moderate_and_small_are_fractions_of_full_move(self):
        ctx = _context_with_implied_move()
        scenarios = build_underlying_scenarios(ctx)
        by_label = {s.label: s for s in scenarios}
        assert by_label["MODERATE_UPSIDE"].move_dollars == ctx.implied_move_dollars * Decimal("0.5")
        assert by_label["SMALL_UPSIDE"].move_dollars == ctx.implied_move_dollars * Decimal("0.25")

    def test_symmetric_grid(self):
        ctx = _context_with_implied_move()
        scenarios = build_underlying_scenarios(ctx)
        by_label = {s.label: s for s in scenarios}
        assert by_label["LARGE_DOWNSIDE"].move_dollars == -by_label["LARGE_UPSIDE"].move_dollars
        assert (
            by_label["MODERATE_DOWNSIDE"].move_dollars == -by_label["MODERATE_UPSIDE"].move_dollars
        )

    def test_none_when_no_evidence_exists(self):
        """Section 4/7's own honesty rule -- no fabricated grid when
        neither implied move nor historical median exists."""
        assert build_underlying_scenarios(_bare_context()) is None

    def test_deterministic_repeatability(self):
        ctx = _context_with_implied_move()
        first = build_underlying_scenarios(ctx)
        second = build_underlying_scenarios(ctx)
        assert first == second


class TestIvScenarios:
    def test_three_named_scenarios(self):
        scenarios = build_iv_scenarios()
        assert [s.label for s in scenarios] == [
            "STRONG_CRUSH",
            "NORMAL_CRUSH",
            "WEAK_CRUSH_OR_ELEVATED",
        ]

    def test_all_marked_heuristic_uncalibrated(self):
        for scenario in build_iv_scenarios():
            assert scenario.source == "HEURISTIC_UNCALIBRATED"

    def test_strong_crush_reduces_iv_most(self):
        scenarios = {s.label: s for s in build_iv_scenarios()}
        assert scenarios["STRONG_CRUSH"].multiplier < scenarios["NORMAL_CRUSH"].multiplier
        assert scenarios["NORMAL_CRUSH"].multiplier < scenarios["WEAK_CRUSH_OR_ELEVATED"].multiplier

    def test_weak_crush_can_mean_iv_increase(self):
        """Real evidence (this project's own n=6 diagnostic, e.g. NVDA)
        shows IV can genuinely INCREASE post-earnings -- the grid must
        allow for that, not assume crush is universal."""
        scenarios = {s.label: s for s in build_iv_scenarios()}
        assert scenarios["WEAK_CRUSH_OR_ELEVATED"].multiplier > Decimal("1")

    def test_always_the_same_fixed_grid(self):
        # build_iv_scenarios() takes no candidate-specific evidence, so
        # it should always return the same fixed grid.
        assert len(build_iv_scenarios()) == len(IV_CRUSH_SCENARIO_GRID) == 3


class TestScenarioLegIv:
    def test_scales_entry_iv_by_multiplier(self):
        iv_scenario = build_iv_scenarios()[0]  # STRONG_CRUSH
        result = scenario_leg_iv(Decimal("0.60"), iv_scenario)
        assert result == Decimal("0.60") * iv_scenario.multiplier

    def test_none_when_no_entry_iv(self):
        iv_scenario = build_iv_scenarios()[0]
        assert scenario_leg_iv(None, iv_scenario) is None

    def test_preserves_relative_skew_across_legs(self):
        """Section 11 -- each leg keeps its OWN entry IV scaled by the
        SAME multiplier, so two legs with different real entry IVs
        retain their real relative difference post-scaling (a limited,
        honest form of skew preservation, never a blanket ATM IV)."""
        iv_scenario = build_iv_scenarios()[1]  # NORMAL_CRUSH
        low_iv_leg = scenario_leg_iv(Decimal("0.40"), iv_scenario)
        high_iv_leg = scenario_leg_iv(Decimal("0.70"), iv_scenario)
        assert low_iv_leg is not None
        assert high_iv_leg is not None
        assert low_iv_leg < high_iv_leg
        # relative ratio preserved exactly (both scaled by the same factor)
        assert (high_iv_leg / low_iv_leg) == Decimal("0.70") / Decimal("0.40")


class TestIvCrushDiagnostic:
    def test_real_n_equals_six_produces_a_real_diagnostic(self):
        """Mirrors this project's own real n=6 paired pre/post-event IV
        observations (DY, HRL, NVDA, SNPS, DG, DLTR) -- descriptive
        only, never consumed by build_iv_scenarios."""
        pairs = [
            (Decimal("0.723"), Decimal("0.522")),
            (Decimal("0.705"), Decimal("0.613")),
            (Decimal("0.771"), Decimal("0.885")),
            (Decimal("1.069"), Decimal("1.000")),
            (Decimal("1.122"), Decimal("0.590")),
            (Decimal("1.156"), Decimal("0.740")),
        ]
        diagnostic = summarize_iv_crush_diagnostic(pairs)
        assert diagnostic is not None
        assert diagnostic.sample_n == 6
        assert diagnostic.min_crush_ratio < 0  # DG-like real crush
        assert diagnostic.max_crush_ratio > 0  # NVDA-like real increase

    def test_none_when_no_pairs(self):
        assert summarize_iv_crush_diagnostic([]) is None

    def test_never_influences_the_heuristic_grid(self):
        """The heuristic grid is a fixed, deterministic module constant
        -- calling the diagnostic first must never change it."""
        before = build_iv_scenarios()
        summarize_iv_crush_diagnostic([(Decimal("1"), Decimal("0.1"))])
        after = build_iv_scenarios()
        assert before == after
