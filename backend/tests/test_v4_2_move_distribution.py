"""V4.2 -- the point-in-time historical post-earnings move distribution.

The invariant these exist to protect is look-ahead safety: a distribution
built for a decision at time T must contain only earnings that had already
reported strictly before T. Everything else here guards against quoting
precision the sample cannot support.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from analytics.earnings.v4_2_move_distribution import (
    QUALITY_DECILES,
    QUALITY_INSUFFICIENT,
    QUALITY_LIMITED,
    QUALITY_QUARTILES,
    TIMING_CLOSE_TO_CLOSE,
    TIMING_UNVERIFIED,
    TIMING_VERIFIED,
    build_move_distribution,
    evidence_quality,
)

D = Decimal
AS_OF = date(2026, 9, 3)


def _moves(*values: str) -> list[Decimal]:
    return [D(v) for v in values]


class TestSampleSizeGovernsPrecision:
    def test_no_sample_yields_no_statistics(self):
        d = build_move_distribution([], as_of=AS_OF)
        assert d.sample_n == 0
        assert d.quality == QUALITY_INSUFFICIENT
        assert d.median_abs_move_pct is None
        assert not d.usable

    def test_a_tiny_sample_gives_a_median_but_no_quartiles(self):
        d = build_move_distribution(_moves("0.05", "-0.07"), as_of=AS_OF)
        assert d.sample_n == 2
        assert d.quality == QUALITY_LIMITED
        assert d.median_abs_move_pct is not None
        assert d.p25_abs_move_pct is None, "four observations are needed before quartiles"
        assert d.p90_abs_move_pct is None

    def test_quartiles_appear_only_at_the_documented_threshold(self):
        d = build_move_distribution(_moves("0.05", "-0.07", "0.09", "-0.03"), as_of=AS_OF)
        assert d.quality == QUALITY_QUARTILES
        assert d.p25_abs_move_pct is not None
        assert d.p75_abs_move_pct is not None
        assert d.p10_abs_move_pct is None, "deciles need a larger sample than quartiles"

    def test_deciles_appear_only_at_the_documented_threshold(self):
        d = build_move_distribution(_moves(*[f"0.0{i}" for i in range(1, 10)] + ["0.11"]),
                                    as_of=AS_OF)
        assert d.sample_n == 10
        assert d.quality == QUALITY_DECILES
        assert d.p10_abs_move_pct is not None
        assert d.p90_abs_move_pct is not None

    @pytest.mark.parametrize("n,expected", [
        (0, QUALITY_INSUFFICIENT), (1, QUALITY_LIMITED), (3, QUALITY_LIMITED),
        (4, QUALITY_QUARTILES), (9, QUALITY_QUARTILES),
        (10, QUALITY_DECILES), (48, QUALITY_DECILES),
    ])
    def test_quality_tiers_match_the_projects_own_thresholds(self, n, expected):
        assert evidence_quality(n) == expected


class TestStatistics:
    def test_magnitude_ignores_direction(self):
        d = build_move_distribution(_moves("0.10", "-0.10"), as_of=AS_OF)
        assert d.median_abs_move_pct == D("0.10")

    def test_direction_frequency_is_reported_separately(self):
        d = build_move_distribution(_moves("0.10", "-0.05", "0.03", "-0.08"), as_of=AS_OF)
        assert d.up_frequency == D("0.5")
        assert d.down_frequency == D("0.5")

    def test_signed_and_absolute_series_are_both_preserved(self):
        d = build_move_distribution(_moves("0.10", "-0.05"), as_of=AS_OF)
        assert d.signed_moves == (D("0.10"), D("-0.05"))
        assert d.abs_moves == (D("0.10"), D("0.05"))

    def test_exceedance_counts_magnitudes_above_a_level(self):
        d = build_move_distribution(_moves("0.05", "-0.15", "0.20", "-0.02"), as_of=AS_OF)
        assert d.exceedance_frequency(D("0.10")) == D("0.5")

    def test_exceedance_is_none_without_a_sample(self):
        assert build_move_distribution([], as_of=AS_OF).exceedance_frequency(D("0.10")) is None


class TestProvenanceTravelsWithTheNumbers:
    def test_the_timing_method_is_always_recorded(self):
        d = build_move_distribution(_moves("0.05"), as_of=AS_OF)
        assert d.timing_method == TIMING_CLOSE_TO_CLOSE

    def test_unverified_announcement_timing_is_stated_not_assumed_away(self):
        d = build_move_distribution(_moves("0.05"), as_of=AS_OF,
                                    timing_provenance=TIMING_UNVERIFIED)
        assert "UNKNOWN" in d.timing_provenance

    def test_verified_timing_is_labelled_differently(self):
        d = build_move_distribution(_moves("0.05"), as_of=AS_OF,
                                    timing_provenance=TIMING_VERIFIED)
        assert d.timing_provenance == TIMING_VERIFIED

    def test_the_point_in_time_boundary_is_carried_on_the_result(self):
        assert build_move_distribution(_moves("0.05"), as_of=AS_OF).as_of == AS_OF

    def test_the_distribution_carries_its_version(self):
        assert build_move_distribution([], as_of=AS_OF).version.startswith("v4_2_move_distribution")


class TestPointInTimeSafety:
    """The query layer is what enforces this; these prove the contract it
    must satisfy, using a fake session so no production DB is touched."""

    def test_an_event_can_never_appear_in_its_own_baseline(self, db_session):
        """Reported strictly before the decision date -- an event reporting
        ON that date has not produced its post-earnings observation yet."""
        from models.company import Company
        from models.earnings_event import EarningsEvent
        from models.price_reaction import PriceReaction
        from services.v4_2_move_history import historical_moves_before

        company = Company(ticker="PITX", name="Point In Time Co")
        db_session.add(company)
        db_session.flush()
        for quarter, (day, move) in enumerate(
            ((date(2026, 3, 1), "0.05"), (date(2026, 6, 1), "0.07"),
             (date(2026, 9, 3), "0.99"), (date(2026, 12, 1), "0.88")), start=1
        ):
            event = EarningsEvent(company_id=company.id, fiscal_year=2026,
                                  fiscal_quarter=quarter, earnings_date=day,
                                  announcement_time="UNKNOWN", date_confirmed=True)
            db_session.add(event)
            db_session.flush()
            db_session.add(PriceReaction(earnings_event_id=event.id,
                                         next_day_move_pct=D(move),
                                         source_provider="test",
                                         retrieved_at=datetime.now(UTC)))
        db_session.flush()

        moves, _ = historical_moves_before(db_session, company.id, date(2026, 9, 3))
        assert D("0.99") not in moves, "the event being decided leaked into its own baseline"
        assert D("0.88") not in moves, "a FUTURE earnings event leaked into the distribution"
        assert sorted(moves) == [D("0.05"), D("0.07")]

    def test_the_same_boundary_always_returns_the_same_sample(self, db_session):
        """Point-in-time reproducibility: the distribution for a past
        decision does not drift as new events are added after it."""
        from models.company import Company
        from models.earnings_event import EarningsEvent
        from models.price_reaction import PriceReaction
        from services.v4_2_move_history import historical_moves_before

        company = Company(ticker="REPX", name="Reproducible Co")
        db_session.add(company)
        db_session.flush()

        def _add(day, move, quarter):
            event = EarningsEvent(company_id=company.id, fiscal_year=2026,
                                  fiscal_quarter=quarter, earnings_date=day,
                                  announcement_time="UNKNOWN", date_confirmed=True)
            db_session.add(event)
            db_session.flush()
            db_session.add(PriceReaction(earnings_event_id=event.id, next_day_move_pct=D(move),
                                         source_provider="test",
                                         retrieved_at=datetime.now(UTC)))
            db_session.flush()

        _add(date(2026, 1, 5), "0.04", 1)
        before, _ = historical_moves_before(db_session, company.id, date(2026, 6, 1))
        _add(date(2026, 7, 1), "0.50", 3)  # a later event, added afterwards
        after, _ = historical_moves_before(db_session, company.id, date(2026, 6, 1))
        assert before == after
