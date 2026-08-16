from datetime import date

from ingestion.earnings_date_backfill import (
    Candidate8K,
    EventPeriod,
    is_earnings_release_8k,
    match_earnings_dates,
)


def test_is_earnings_release_8k():
    assert is_earnings_release_8k("2.02,9.01") is True
    assert is_earnings_release_8k("5.02") is False
    assert is_earnings_release_8k(None) is False
    assert is_earnings_release_8k("") is False


def test_match_earnings_dates_picks_nearest_qualifying_8k():
    events = [EventPeriod(key="Q1", period_end_date=date(2025, 3, 31))]
    filings = [
        Candidate8K("acc-early", date(2025, 4, 5), "5.02"),  # wrong item, not earnings
        Candidate8K("acc-good", date(2025, 4, 24), "2.02,9.01"),  # 24 days after — qualifies
        Candidate8K("acc-late", date(2025, 6, 1), "2.02"),  # too late (>60d), wrong quarter
    ]

    matches = match_earnings_dates(events, filings)

    assert matches["Q1"].accession_number == "acc-good"


def test_match_earnings_dates_out_of_window_is_unmatched():
    events = [EventPeriod(key="Q1", period_end_date=date(2025, 3, 31))]
    filings = [Candidate8K("acc-too-soon", date(2025, 4, 2), "2.02")]  # 2 days: below MIN_LAG

    matches = match_earnings_dates(events, filings)

    assert "Q1" not in matches


def test_match_earnings_dates_each_8k_used_at_most_once():
    events = [
        EventPeriod(key="Q1", period_end_date=date(2025, 3, 31)),
        EventPeriod(key="Q2", period_end_date=date(2025, 4, 5)),  # implausibly close to Q1
    ]
    filings = [Candidate8K("acc-shared", date(2025, 4, 24), "2.02")]

    matches = match_earnings_dates(events, filings)

    # Only one event can claim the single qualifying 8-K.
    assert len(matches) == 1
    assert set(matches) <= {"Q1", "Q2"}


def test_match_earnings_dates_multiple_quarters_each_get_their_own_8k():
    events = [
        EventPeriod(key="Q1", period_end_date=date(2025, 3, 31)),
        EventPeriod(key="Q2", period_end_date=date(2025, 6, 30)),
    ]
    filings = [
        Candidate8K("acc-q1", date(2025, 4, 24), "2.02"),
        Candidate8K("acc-q2", date(2025, 7, 23), "2.02"),
    ]

    matches = match_earnings_dates(events, filings)

    assert matches["Q1"].accession_number == "acc-q1"
    assert matches["Q2"].accession_number == "acc-q2"
