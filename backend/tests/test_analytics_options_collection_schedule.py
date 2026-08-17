from datetime import date

from analytics.options.collection_schedule import should_collect_snapshot

EARNINGS_DATE = date(2026, 9, 22)


def test_collects_on_each_default_offset():
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 9, 8)) is True  # T-14
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 9, 15)) is True  # T-7
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 9, 19)) is True  # T-3
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 9, 21)) is True  # T-1


def test_does_not_collect_on_off_days():
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 9, 10)) is False
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 9, 20)) is False


def test_does_not_collect_on_earnings_date_itself():
    assert should_collect_snapshot(EARNINGS_DATE, EARNINGS_DATE) is False


def test_does_not_collect_after_earnings_date():
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 9, 23)) is False


def test_does_not_collect_far_before_earnings_date():
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 8, 1)) is False


def test_respects_custom_offsets():
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 9, 20), offsets=(2,)) is True
    assert should_collect_snapshot(EARNINGS_DATE, date(2026, 9, 8), offsets=(2,)) is False
