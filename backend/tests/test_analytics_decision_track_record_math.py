"""Phase 4.6 -- table-driven tests for analytics/decision/track_record_math.py."""

from decimal import Decimal

from analytics.decision.track_record_math import (
    DTE_BUCKETS,
    PROBABILITY_BUCKETS,
    Rate,
    compute_average,
    compute_equity_curve,
    compute_max_drawdown,
    compute_median,
    compute_profit_factor,
    dte_bucket_label,
    probability_bucket_label,
    rate_from_bools,
)


def test_rate_pct_is_none_for_zero_total():
    rate = Rate(correct=0, total=0)
    assert rate.pct is None


def test_rate_pct_is_a_fraction():
    rate = Rate(correct=3, total=4)
    assert rate.pct == Decimal("0.75")


def test_rate_from_bools():
    rate = rate_from_bools([True, False, True, True])
    assert rate.correct == 3
    assert rate.total == 4


def test_compute_average_empty_is_none():
    assert compute_average([]) is None


def test_compute_average():
    assert compute_average([Decimal("1"), Decimal("2"), Decimal("3")]) == Decimal("2")


def test_compute_median_empty_is_none():
    assert compute_median([]) is None


def test_compute_median_odd_count():
    assert compute_median([Decimal("1"), Decimal("5"), Decimal("2")]) == Decimal("2")


def test_compute_median_even_count_averages_middle_two():
    assert compute_median([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]) == Decimal(
        "2.5"
    )


def test_profit_factor_none_when_no_losses():
    assert compute_profit_factor([Decimal("100"), Decimal("50")]) is None


def test_profit_factor_none_for_empty():
    assert compute_profit_factor([]) is None


def test_profit_factor_hand_computed():
    # gross profit 150, gross loss 50 -> 3.0
    pnls = [Decimal("100"), Decimal("50"), Decimal("-30"), Decimal("-20")]
    assert compute_profit_factor(pnls) == Decimal("3")


# --------------------------------------------------------------------------
# Equity curve / max drawdown (Phase 4.6 approved decision 1 -- real dollar
# equity curve against the fixed benchmark capital, never R-multiples)
# --------------------------------------------------------------------------


def test_max_drawdown_empty_is_none_not_zero():
    """Zero settled trades -> None (no data), not a fabricated 0 --
    distinct from 'there were trades and none of them drew down' (also
    0, but a real, computed 0)."""
    result = compute_max_drawdown(Decimal("2000"), [])
    assert result.max_drawdown is None
    assert result.max_drawdown_pct is None


def test_max_drawdown_all_wins_is_a_real_zero():
    result = compute_max_drawdown(Decimal("2000"), [Decimal("100"), Decimal("200")])
    assert result.max_drawdown == Decimal("0")
    assert result.max_drawdown_pct == Decimal("0")


def test_equity_curve_is_cumulative():
    curve = compute_equity_curve(Decimal("2000"), [Decimal("100"), Decimal("-300"), Decimal("50")])
    assert curve == [Decimal("2100"), Decimal("1800"), Decimal("1850")]


def test_max_drawdown_hand_computed():
    """2000 -> 2100 (peak) -> 1800 (drawdown 300 off the 2100 peak) ->
    1850 (still below peak, not a new trough) -> 2200 (new peak, no
    further drawdown). Max drawdown is 300 off a 2100 peak = 14.2857...%."""
    pnls = [Decimal("100"), Decimal("-300"), Decimal("50"), Decimal("350")]
    result = compute_max_drawdown(Decimal("2000"), pnls)
    assert result.max_drawdown == Decimal("300")
    assert result.max_drawdown_pct == Decimal("300") / Decimal("2100") * 100


def test_max_drawdown_never_recovering_uses_the_final_trough():
    pnls = [Decimal("-100"), Decimal("-200"), Decimal("-50")]
    result = compute_max_drawdown(Decimal("2000"), pnls)
    assert result.max_drawdown == Decimal("350")  # 2000 peak, 1650 trough
    assert result.max_drawdown_pct == Decimal("350") / Decimal("2000") * 100


# --------------------------------------------------------------------------
# DTE buckets (Phase 4.6 approved decision 3)
# --------------------------------------------------------------------------


def test_dte_buckets_are_contiguous_and_non_overlapping():
    """Every integer from 0 to 60 lands in exactly one bucket -- no gaps,
    no double-matches."""
    for dte in range(0, 61):
        matches = [
            label
            for label, lower, upper in DTE_BUCKETS
            if lower <= dte and (upper is None or dte <= upper)
        ]
        assert len(matches) == 1, f"dte={dte} matched {matches}"


def test_dte_bucket_boundaries():
    assert dte_bucket_label(0) == "0-3"
    assert dte_bucket_label(3) == "0-3"
    assert dte_bucket_label(4) == "4-7"
    assert dte_bucket_label(7) == "4-7"
    assert dte_bucket_label(8) == "8-14"
    assert dte_bucket_label(14) == "8-14"
    assert dte_bucket_label(15) == "15-30"
    assert dte_bucket_label(30) == "15-30"
    assert dte_bucket_label(31) == "30+"
    assert dte_bucket_label(100) == "30+"


def test_dte_bucket_negative_is_none():
    assert dte_bucket_label(-1) is None


# --------------------------------------------------------------------------
# Probability / confidence buckets (Phase 4.6 approved decision 5 -- five
# buckets including <60%)
# --------------------------------------------------------------------------


def test_probability_buckets_are_contiguous_and_non_overlapping():
    for pct in range(0, 101):
        p = Decimal(pct) / 100
        label = probability_bucket_label(p)
        assert label in {b[0] for b in PROBABILITY_BUCKETS}


def test_probability_bucket_boundaries():
    assert probability_bucket_label(Decimal("0.00")) == "<60%"
    assert probability_bucket_label(Decimal("0.59")) == "<60%"
    assert probability_bucket_label(Decimal("0.60")) == "60-70%"
    assert probability_bucket_label(Decimal("0.69")) == "60-70%"
    assert probability_bucket_label(Decimal("0.70")) == "70-80%"
    assert probability_bucket_label(Decimal("0.79")) == "70-80%"
    assert probability_bucket_label(Decimal("0.80")) == "80-90%"
    assert probability_bucket_label(Decimal("0.89")) == "80-90%"
    assert probability_bucket_label(Decimal("0.90")) == "90%+"
    assert probability_bucket_label(Decimal("1.00")) == "90%+"
