"""V4.3.1 chain-coverage tests (2026-09-03). Verifies the engine never
conflates "not present in a narrow captured window" with "does not
exist in the real listed chain" -- this task's own Section 4 mandate,
and the direct fix for the DG misclassification (Section 21)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_chain_coverage import (
    CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW,
    ChainMetadata,
    assess_protective_wing_coverage,
    assess_target_coverage,
    historical_replay_status,
)

EXP = date(2026, 9, 18)
NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _metadata(source, strikes=(90, 95, 100, 105, 110), window=None) -> ChainMetadata:
    decimals = tuple(Decimal(s) for s in strikes)
    return ChainMetadata(
        ticker="ZZ",
        expiration=EXP,
        observed_at=NOW,
        call_strikes=decimals,
        put_strikes=decimals,
        source=source,
        captured_window_size=window,
    )


class TestTargetCoverage:
    def test_within_range_resolves(self):
        a = assess_target_coverage(Decimal("103"), "call", _metadata("captured_window"))
        assert a.status == "TARGET_RESOLVED"
        assert a.nearest_listed_strike == Decimal("105")

    def test_beyond_complete_metadata_is_confirmed_not_listed(self):
        a = assess_target_coverage(Decimal("150"), "call", _metadata("complete_listed"))
        assert a.status == "TARGET_NOT_LISTED"
        assert "confirmed" in a.reason.lower() or "genuine" in a.reason.lower()

    def test_beyond_captured_window_is_ambiguous_not_confirmed(self):
        a = assess_target_coverage(Decimal("150"), "call", _metadata("captured_window", window=5))
        assert a.status == "TARGET_BEYOND_CAPTURED_WINDOW"
        assert "not" in a.reason.lower()  # explicitly disclaims confirming absence

    def test_beyond_unknown_source_degrades_conservatively(self):
        a = assess_target_coverage(Decimal("150"), "call", _metadata("unknown"))
        assert a.status == "TARGET_BEYOND_CAPTURED_WINDOW"

    def test_beyond_synthetic_source_degrades_conservatively(self):
        a = assess_target_coverage(Decimal("150"), "call", _metadata("synthetic"))
        assert a.status == "TARGET_BEYOND_CAPTURED_WINDOW"

    def test_no_strikes_at_all_complete_metadata(self):
        a = assess_target_coverage(Decimal("100"), "put", _metadata("complete_listed", strikes=()))
        assert a.status == "TARGET_NOT_LISTED"

    def test_no_strikes_at_all_captured_window(self):
        a = assess_target_coverage(Decimal("100"), "put", _metadata("captured_window", strikes=()))
        assert a.status == "TARGET_BEYOND_CAPTURED_WINDOW"


class TestProtectiveWingCoverage:
    def test_wing_found_resolves(self):
        a = assess_protective_wing_coverage(
            Decimal("105"), "call", "up", _metadata("captured_window")
        )
        assert a.status == "TARGET_RESOLVED"
        assert a.nearest_listed_strike == Decimal("110")

    def test_no_wing_confirmed_absent_with_complete_metadata(self):
        """The DG shape, directly: short strike at the chain's own
        edge, complete metadata confirms nothing exists further out."""
        a = assess_protective_wing_coverage(
            Decimal("110"), "call", "up", _metadata("complete_listed")
        )
        assert a.status == "NO_PROTECTIVE_WING_LISTED"

    def test_no_wing_ambiguous_with_captured_window(self):
        """The DG shape as it ACTUALLY occurred in V4.3's replay: short
        strike at the captured window's own edge -- ambiguous, not
        confirmed."""
        a = assess_protective_wing_coverage(
            Decimal("110"), "call", "up", _metadata("captured_window", window=5)
        )
        assert a.status == "TARGET_BEYOND_CAPTURED_WINDOW"


class TestHistoricalReplayStatus:
    def test_captured_window_ambiguity_becomes_cannot_verify(self):
        a = assess_target_coverage(Decimal("150"), "call", _metadata("captured_window", window=5))
        assert historical_replay_status(a) == CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW

    def test_wing_ambiguity_becomes_cannot_verify(self):
        a = assess_protective_wing_coverage(
            Decimal("110"), "call", "up", _metadata("captured_window", window=5)
        )
        assert historical_replay_status(a) == CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW

    def test_complete_metadata_absence_is_never_softened(self):
        """A genuinely confirmed absence (complete metadata) must NOT
        be diluted into the ambiguous historical caveat -- this
        function never manufactures uncertainty where real confirmed
        data exists."""
        a = assess_target_coverage(Decimal("150"), "call", _metadata("complete_listed"))
        assert historical_replay_status(a) == "TARGET_NOT_LISTED"

    def test_resolved_target_passes_through_unchanged(self):
        a = assess_target_coverage(Decimal("103"), "call", _metadata("captured_window"))
        assert historical_replay_status(a) == "TARGET_RESOLVED"

    def test_never_uses_live_data_to_resolve_a_historical_ambiguity(self):
        """Section 21's own explicit rule: no amount of separately-
        supplied "current" metadata can resolve a historical
        assessment -- the classification is a pure function of the
        ORIGINAL assessment's own metadata_source, never re-checked
        against anything else."""
        historical_assessment = assess_target_coverage(
            Decimal("150"), "call", _metadata("captured_window", window=5)
        )
        # A live re-check against complete metadata is a SEPARATE,
        # clearly distinct assessment -- never substituted in place of
        # the historical one.
        live_assessment = assess_target_coverage(
            Decimal("150"), "call", _metadata("complete_listed")
        )
        assert (
            historical_replay_status(historical_assessment) == CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW
        )
        assert live_assessment.status == "TARGET_NOT_LISTED"
        assert historical_assessment is not live_assessment
