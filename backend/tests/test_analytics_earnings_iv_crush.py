from decimal import Decimal

import pytest

from analytics.earnings.iv_crush import (
    HistoryRecord,
    calculate_iv_crush,
    compare_implied_vs_realised,
    summarize_history,
)

D = Decimal


def test_calculate_iv_crush_typical_post_earnings_drop():
    result = calculate_iv_crush(pre_event_iv=D("0.65"), post_event_iv=D("0.35"))

    assert result.absolute_change == D("-0.30")
    assert result.relative_crush_pct == D("-0.30") / D("0.65")


def test_calculate_iv_crush_rejects_nonpositive_pre_iv():
    with pytest.raises(ValueError):
        calculate_iv_crush(pre_event_iv=D("0"), post_event_iv=D("0.3"))


def test_compare_implied_vs_realised_underpriced():
    result = compare_implied_vs_realised(implied_move_pct=D("0.06"), realised_move_pct=D("0.12"))
    assert result.verdict == "underpriced"
    assert result.error == D("0.06")


def test_compare_implied_vs_realised_overpriced():
    result = compare_implied_vs_realised(implied_move_pct=D("0.10"), realised_move_pct=D("0.03"))
    assert result.verdict == "overpriced"


def test_compare_implied_vs_realised_accurate_within_threshold():
    result = compare_implied_vs_realised(implied_move_pct=D("0.08"), realised_move_pct=D("0.083"))
    assert result.verdict == "accurate"


def test_summarize_history():
    records = [
        HistoryRecord(D("0.06"), D("0.12"), D("0.65"), D("0.35")),  # underpriced
        HistoryRecord(D("0.10"), D("0.03"), D("0.60"), D("0.30")),  # overpriced
        HistoryRecord(D("0.08"), D("0.08"), D("0.55"), D("0.28")),  # accurate
    ]

    summary = summarize_history(records)

    assert summary.event_count == 3
    assert summary.underpriced_count == 1
    assert summary.overpriced_count == 1
    assert summary.accurate_count == 1
    assert summary.average_implied_move_pct == (D("0.06") + D("0.10") + D("0.08")) / 3
    assert summary.average_iv_crush_pct is not None


def test_summarize_history_handles_missing_iv_gracefully():
    records = [
        HistoryRecord(D("0.06"), D("0.12"), None, None),  # no IV data for this one
        HistoryRecord(D("0.08"), D("0.08"), D("0.55"), D("0.28")),
    ]

    summary = summarize_history(records)

    assert summary.event_count == 2
    assert summary.average_iv_crush_pct is not None  # computed from the one record that has it


def test_summarize_history_rejects_empty():
    with pytest.raises(ValueError):
        summarize_history([])
