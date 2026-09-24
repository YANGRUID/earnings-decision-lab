"""The normal 15:30 settlement path records WHICH executable sides priced it.

Found latent (2026-09-24 audit). That path only settles when every required
side carried a real executable value -- anything else is written as
OBSERVATION_FAILED -- so its settlements were correctly graded EXECUTABLE. But
they were graded from the ABSENCE of a pricing_method, under a rule whose
stated reason ("written before the end-of-day fallback existed") stopped being
true the day the fallback shipped. Any later path writing a settlement without
a method would have inherited an executable grade it never earned.
"""

from services.v4_settlement_fallback import PRICING_EXECUTABLE_ASK, PRICING_EXECUTABLE_BID
from services.v4_settlement_quality import GRADE_EXECUTABLE, settlement_grade
from services.v4_shadow_cohort import _executable_pricing_method


class _Obs:
    def __init__(self, legs):
        self.legs_json = {"legs": legs} if legs is not None else None


class _Settlement:
    def __init__(self, pricing_method, status="SETTLED"):
        self.pricing_method = pricing_method
        self.status = status


class TestTheRecordedMethod:
    def test_a_long_only_close_records_the_bid_side(self):
        method = _executable_pricing_method(_Obs([{"required_side": "bid"}]))
        assert method == PRICING_EXECUTABLE_BID

    def test_a_short_only_close_records_the_ask_side(self):
        method = _executable_pricing_method(_Obs([{"required_side": "ask"}]))
        assert method == PRICING_EXECUTABLE_ASK

    def test_a_spread_records_both_sides_in_the_established_joined_form(self):
        """Matches what the end-of-day fallback already writes, e.g.
        "EXECUTABLE_ASK+EXECUTABLE_BID+MARKET_CLOSE_FALLBACK"."""
        method = _executable_pricing_method(
            _Obs([{"required_side": "bid"}, {"required_side": "ask"}, {"required_side": "bid"}])
        )
        assert method == f"{PRICING_EXECUTABLE_ASK}+{PRICING_EXECUTABLE_BID}"

    def test_no_observation_or_no_legs_records_nothing(self):
        assert _executable_pricing_method(None) is None
        assert _executable_pricing_method(_Obs(None)) is None
        assert _executable_pricing_method(_Obs([])) is None


class TestTheGradeIsUnchanged:
    def test_the_recorded_method_still_grades_executable(self):
        """The fix records evidence; it must not move any number."""
        for method in (
            PRICING_EXECUTABLE_BID,
            PRICING_EXECUTABLE_ASK,
            f"{PRICING_EXECUTABLE_ASK}+{PRICING_EXECUTABLE_BID}",
        ):
            assert settlement_grade(_Settlement(method)) == GRADE_EXECUTABLE

    def test_legacy_null_rows_keep_grading_executable(self):
        """Historical rows are not rewritten and must keep their grade."""
        assert settlement_grade(_Settlement(None)) == GRADE_EXECUTABLE
