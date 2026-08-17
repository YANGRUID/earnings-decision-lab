from decimal import Decimal

from analytics.earnings.guidance_comparison import compare_guidance, compare_ranges
from schemas.extraction import GuidanceExtraction, RevenueGuidance

D = Decimal


def test_compare_ranges_computes_midpoint_change_and_pct():
    previous = RevenueGuidance(low=D("100"), high=D("120"))  # midpoint 110
    current = RevenueGuidance(low=D("110"), high=D("130"))  # midpoint 120

    result = compare_ranges(previous, current)

    assert result.previous_midpoint == D("110")
    assert result.current_midpoint == D("120")
    assert result.midpoint_change == D("10")
    assert result.midpoint_change_pct == D("10") / D("110")


def test_compare_ranges_handles_missing_previous():
    current = RevenueGuidance(low=D("100"), high=D("120"))

    result = compare_ranges(None, current)

    assert result.previous_midpoint is None
    assert result.current_midpoint == D("110")
    assert result.midpoint_change is None
    assert result.midpoint_change_pct is None


def test_compare_ranges_handles_both_missing():
    result = compare_ranges(None, None)
    assert result.midpoint_change is None
    assert result.midpoint_change_pct is None


def test_compare_ranges_negative_previous_midpoint_uses_absolute_value_for_pct():
    # A guided loss narrowing toward breakeven: pct change should be
    # interpretable (dividing by abs(), not the negative raw value).
    previous = RevenueGuidance(low=D("-10"), high=D("-6"))  # midpoint -8
    current = RevenueGuidance(low=D("-4"), high=D("0"))  # midpoint -2

    result = compare_ranges(previous, current)

    assert result.midpoint_change == D("6")  # -2 - (-8)
    assert result.midpoint_change_pct == D("6") / D("8")


def test_compare_guidance_covers_all_four_metrics():
    previous = GuidanceExtraction(revenue=RevenueGuidance(low=D("100"), high=D("120")))
    current = GuidanceExtraction(revenue=RevenueGuidance(low=D("110"), high=D("130")))

    comparison = compare_guidance(previous, current)

    assert comparison.revenue.midpoint_change == D("10")
    assert comparison.eps.previous_midpoint is None
    assert comparison.gross_margin.previous_midpoint is None
    assert comparison.capex.previous_midpoint is None
