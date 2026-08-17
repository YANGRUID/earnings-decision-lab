from decimal import Decimal

from analytics.earnings.historical_moves import historical_move_stats


def test_returns_none_for_empty_history():
    assert historical_move_stats([]) is None


def test_single_move_stats():
    result = historical_move_stats([Decimal("-0.05")])

    assert result is not None
    assert result.sample_size == 1
    assert result.average_abs_move_pct == Decimal("0.05")
    assert result.median_abs_move_pct == Decimal("0.05")
    assert result.largest_abs_move_pct == Decimal("0.05")
    assert result.largest_move_pct_signed == Decimal("-0.05")


def test_average_and_median_computed_over_absolute_values():
    moves = [Decimal("0.02"), Decimal("-0.08"), Decimal("0.05")]
    result = historical_move_stats(moves)

    assert result is not None
    assert result.sample_size == 3
    assert result.average_abs_move_pct == (Decimal("0.02") + Decimal("0.08") + Decimal("0.05")) / 3
    assert result.median_abs_move_pct == Decimal("0.05")


def test_largest_move_preserves_sign_of_biggest_magnitude():
    moves = [Decimal("0.03"), Decimal("-0.10"), Decimal("0.07")]
    result = historical_move_stats(moves)

    assert result is not None
    assert result.largest_abs_move_pct == Decimal("0.10")
    assert result.largest_move_pct_signed == Decimal("-0.10")


def test_even_count_median_averages_middle_two():
    moves = [Decimal("0.02"), Decimal("0.04"), Decimal("0.06"), Decimal("0.08")]
    result = historical_move_stats(moves)

    assert result is not None
    assert result.median_abs_move_pct == Decimal("0.05")
