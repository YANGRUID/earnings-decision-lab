from decimal import Decimal

from analytics.decision.probability import (
    LOW_SAMPLE_THRESHOLD,
    build_estimated_probability,
    wilson_confidence_interval,
)
from analytics.options.move_compatibility import MoveCompatibility


def _round(value: Decimal, digits: int = 3) -> float:
    return round(float(value), digits)


class TestWilsonConfidenceInterval:
    def test_none_when_sample_size_zero(self):
        assert wilson_confidence_interval(0, 0) is None

    def test_known_reference_values_13_of_18(self):
        # Cross-checked against a standard Wilson score calculator:
        # 13/18 (72.2%) -> 95% CI approximately [49.1%, 87.5%].
        lower, upper = wilson_confidence_interval(13, 18)
        assert _round(lower, 2) == 0.49
        assert _round(upper, 2) == 0.88

    def test_known_reference_values_all_successes(self):
        # 5/5 -> Wilson interval never collapses to exactly [1, 1], unlike
        # the naive normal approximation -- stays a real, finite interval.
        lower, upper = wilson_confidence_interval(5, 5)
        assert lower > Decimal("0.5")
        assert upper == Decimal("1")

    def test_known_reference_values_all_failures(self):
        lower, upper = wilson_confidence_interval(0, 5)
        assert lower == Decimal("0")
        assert upper < Decimal("0.5")

    def test_interval_always_within_unit_range(self):
        for n in (1, 2, 5, 10, 18, 50, 100):
            for successes in range(n + 1):
                lower, upper = wilson_confidence_interval(successes, n)
                assert Decimal("0") <= lower <= upper <= Decimal("1")

    def test_wider_sample_narrows_interval(self):
        small_lower, small_upper = wilson_confidence_interval(13, 18)
        large_lower, large_upper = wilson_confidence_interval(130, 180)
        assert (large_upper - large_lower) < (small_upper - small_lower)

    def test_invalid_successes_raises(self):
        import pytest

        with pytest.raises(ValueError):
            wilson_confidence_interval(-1, 10)
        with pytest.raises(ValueError):
            wilson_confidence_interval(11, 10)


class TestBuildEstimatedProbability:
    def _compat(self, sample_size: int, compatible_count: int) -> MoveCompatibility:
        return MoveCompatibility(
            method="historical_move_compatibility",
            sample_size=sample_size,
            requires_move_beyond_threshold=True,
            required_move_pct=Decimal("0.05"),
            compatible_count=compatible_count,
            compatible_pct=Decimal(compatible_count) / Decimal(sample_size),
            historical_moves_pct=tuple(Decimal("0.05") for _ in range(sample_size)),
        )

    def test_none_when_move_compatibility_is_none(self):
        assert build_estimated_probability(None) is None

    def test_wraps_the_same_probability_never_recomputes(self):
        compat = self._compat(18, 13)
        result = build_estimated_probability(compat)
        assert result is not None
        assert result.probability == compat.compatible_pct
        assert result.sample_size == 18
        assert result.compatible_count == 13

    def test_flags_low_sample_confidence_below_threshold(self):
        small = self._compat(LOW_SAMPLE_THRESHOLD - 1, 5)
        result = build_estimated_probability(small)
        assert result is not None
        assert result.low_sample_confidence is True

    def test_does_not_flag_low_sample_at_or_above_threshold(self):
        big = self._compat(LOW_SAMPLE_THRESHOLD, 10)
        result = build_estimated_probability(big)
        assert result is not None
        assert result.low_sample_confidence is False

    def test_attaches_a_real_confidence_interval(self):
        compat = self._compat(18, 13)
        result = build_estimated_probability(compat)
        assert result is not None
        assert result.wilson_lower is not None
        assert result.wilson_upper is not None
        assert result.wilson_lower < result.probability < result.wilson_upper
