"""V4.2 -- versioned AMC/BMO anchoring of the post-earnings move.

The defect being corrected: the shared price_reaction_moves() never sees the
announcement time, so a BMO report is measured from its own POST-release
close. These pin the corrected anchoring per classification, and pin that an
unknown timing is never quietly promoted to a known one.
"""

from datetime import date
from decimal import Decimal

import pytest

from analytics.earnings.v4_2_reaction_anchoring import (
    KNOWN_AMC,
    KNOWN_BMO,
    TIMING_MIXED,
    TIMING_UNVERIFIED,
    TIMING_VERIFIED,
    UNKNOWN_TIMING,
    aggregate_timing_quality,
    anchored_reaction,
    classify_announcement_time,
)

D = Decimal

# Mon 31 Aug .. Fri 4 Sep, with the earnings date on Wednesday 2 Sep.
BARS = {
    date(2026, 8, 31): D("100"),
    date(2026, 9, 1): D("102"),
    date(2026, 9, 2): D("110"),
    date(2026, 9, 3): D("121"),
    date(2026, 9, 4): D("125"),
}
EARNINGS = date(2026, 9, 2)


class TestClassification:
    @pytest.mark.parametrize("raw,expected", [
        ("AFTER_MARKET", KNOWN_AMC),
        ("BEFORE_MARKET", KNOWN_BMO),
        ("UNKNOWN", UNKNOWN_TIMING),
    ])
    def test_the_projects_enum_values_map_to_classifications(self, raw, expected):
        assert classify_announcement_time(raw) == expected

    def test_an_unrecognised_value_is_unknown_not_an_assumption(self):
        assert classify_announcement_time("SOMETHING_NEW") == UNKNOWN_TIMING

    def test_none_is_unknown(self):
        assert classify_announcement_time(None) == UNKNOWN_TIMING


class TestKnownAmc:
    def test_it_anchors_on_the_pre_release_close_and_the_next_session(self):
        """AMC releases after D0's close, so D0 close is the last price
        before the news and D+1 close is the reaction."""
        out = anchored_reaction(BARS, earnings_date=EARNINGS, timing_classification=KNOWN_AMC)
        assert out is not None
        assert out.pre_event_date == date(2026, 9, 2)
        assert out.post_event_date == date(2026, 9, 3)
        assert out.signed_move_pct == D("0.1")
        assert out.timing_quality == TIMING_VERIFIED


class TestKnownBmo:
    def test_it_anchors_on_the_previous_session_and_the_earnings_day(self):
        """BMO releases before D0's open, so D-1 close is the last price
        before the news and D0's own close is the reaction. The AMC rule
        would have used D0 -- a POST-release price -- as the baseline."""
        out = anchored_reaction(BARS, earnings_date=EARNINGS, timing_classification=KNOWN_BMO)
        assert out is not None
        assert out.pre_event_date == date(2026, 9, 1)
        assert out.post_event_date == date(2026, 9, 2)
        assert out.timing_quality == TIMING_VERIFIED

    def test_bmo_and_amc_genuinely_disagree_on_the_same_event(self):
        amc = anchored_reaction(BARS, earnings_date=EARNINGS, timing_classification=KNOWN_AMC)
        bmo = anchored_reaction(BARS, earnings_date=EARNINGS, timing_classification=KNOWN_BMO)
        assert amc is not None and bmo is not None
        assert amc.signed_move_pct != bmo.signed_move_pct
        assert bmo.post_event_date < amc.post_event_date

    def test_bmo_never_uses_the_d0_to_d1_window(self):
        out = anchored_reaction(BARS, earnings_date=EARNINGS, timing_classification=KNOWN_BMO)
        assert out is not None
        assert not (out.pre_event_date == EARNINGS and out.post_event_date == date(2026, 9, 3))

    def test_a_non_trading_earnings_date_still_anchors_on_real_sessions(self):
        """Earnings dated on a Saturday: the reaction session is the next
        real session in the bars, not an invented one."""
        out = anchored_reaction(
            BARS, earnings_date=date(2026, 9, 5), timing_classification=KNOWN_BMO
        )
        assert out is None, "no session on/after the date exists in these bars"


class TestUnknownTiming:
    def test_it_uses_the_amc_convention_but_is_flagged_unverified(self):
        """Switching convention silently would make the new numbers
        incomparable with the corpus built under the old one."""
        out = anchored_reaction(BARS, earnings_date=EARNINGS, timing_classification=UNKNOWN_TIMING)
        amc = anchored_reaction(BARS, earnings_date=EARNINGS, timing_classification=KNOWN_AMC)
        assert out is not None and amc is not None
        assert out.signed_move_pct == amc.signed_move_pct
        assert out.timing_quality == TIMING_UNVERIFIED
        assert not out.timing_verified

    def test_timing_is_never_inferred_from_price_behaviour(self):
        """A violent gap does not promote an unknown event to a known one."""
        gappy = dict(BARS)
        gappy[date(2026, 9, 3)] = D("180")
        out = anchored_reaction(gappy, earnings_date=EARNINGS,
                                timing_classification=UNKNOWN_TIMING)
        assert out is not None
        assert out.timing_classification == UNKNOWN_TIMING
        assert out.timing_quality == TIMING_UNVERIFIED


class TestRefusalRatherThanSubstitution:
    def test_no_bars_yields_nothing(self):
        assert anchored_reaction({}, earnings_date=EARNINGS,
                                 timing_classification=KNOWN_AMC) is None

    def test_a_missing_post_event_session_yields_nothing(self):
        only_before = {d: p for d, p in BARS.items() if d <= EARNINGS}
        assert anchored_reaction(only_before, earnings_date=EARNINGS,
                                 timing_classification=KNOWN_AMC) is None

    def test_a_missing_pre_event_session_yields_nothing(self):
        only_after = {d: p for d, p in BARS.items() if d > EARNINGS}
        assert anchored_reaction(only_after, earnings_date=EARNINGS,
                                 timing_classification=KNOWN_AMC) is None

    def test_a_non_positive_base_price_yields_nothing(self):
        broken = dict(BARS)
        broken[EARNINGS] = D("0")
        assert anchored_reaction(broken, earnings_date=EARNINGS,
                                 timing_classification=KNOWN_AMC) is None


class TestAggregateTimingQuality:
    def _r(self, classification):
        return anchored_reaction(BARS, earnings_date=EARNINGS,
                                 timing_classification=classification)

    def test_all_verified_is_verified(self):
        assert aggregate_timing_quality([self._r(KNOWN_AMC), self._r(KNOWN_BMO)]) == (
            TIMING_VERIFIED
        )

    def test_all_unknown_is_unverified(self):
        assert aggregate_timing_quality([self._r(UNKNOWN_TIMING)]) == TIMING_UNVERIFIED

    def test_one_unknown_contaminates_the_set(self):
        """Conservative by design: a single mis-anchored event distorts the
        magnitudes just as effectively as many."""
        assert aggregate_timing_quality(
            [self._r(KNOWN_AMC), self._r(UNKNOWN_TIMING)]
        ) == TIMING_MIXED

    def test_an_empty_set_is_unverified_not_verified(self):
        assert aggregate_timing_quality([]) == TIMING_UNVERIFIED
