"""Settlement evidence grading, and the analytics filter built on it.

A closing mark is not a fill. v4.1.0 can settle a position whose executable
side was empty, which is what stops positions being stranded -- and it means
"SETTLED" alone no longer says the exit price was transactable. These pin the
grading, the worst-wins rule for multi-leg settlements, and the guarantee
that filtering never loses a row.
"""

from types import SimpleNamespace

from services.v4_settlement_quality import (
    GRADE_EXECUTABLE,
    GRADE_INTRINSIC,
    GRADE_MARKET_CLOSE,
    GRADE_UNRESOLVED,
    executable_only,
    settlement_grade,
    summarize_settlement_quality,
)


def _s(status="SETTLED", method=None, key="v4_2k_moderate"):
    return SimpleNamespace(
        status=status, pricing_method=method, configuration_key=key, realized_pnl=None
    )


class TestGrading:
    def test_a_settlement_written_before_the_fallback_existed_is_executable(self):
        """Those could only be written when every required side was present."""
        assert settlement_grade(_s(method=None)) == GRADE_EXECUTABLE

    def test_all_executable_legs_grade_executable(self):
        assert settlement_grade(_s(method="EXECUTABLE_ASK+EXECUTABLE_BID")) == GRADE_EXECUTABLE

    def test_one_closing_mark_leg_downgrades_the_whole_settlement(self):
        """Worst wins: the net exit value is no longer transactable."""
        assert (
            settlement_grade(_s(method="EXECUTABLE_BID+MARKET_CLOSE_FALLBACK"))
            == GRADE_MARKET_CLOSE
        )

    def test_intrinsic_outranks_a_closing_mark_in_severity(self):
        assert (
            settlement_grade(_s(method="MARKET_CLOSE_FALLBACK+EXPIRATION_INTRINSIC_AT_CLOSE"))
            == GRADE_INTRINSIC
        )

    def test_an_unsettled_row_is_unresolved_whatever_it_was_priced_with(self):
        assert settlement_grade(_s(status="OBSERVATION_FAILED", method="EXECUTABLE_BID")) == (
            GRADE_UNRESOLVED
        )


class TestBreakdown:
    def test_counts_and_rates_describe_the_same_set(self):
        rows = [
            _s(method=None),
            _s(method="EXECUTABLE_BID"),
            _s(method="EXECUTABLE_BID+MARKET_CLOSE_FALLBACK"),
            _s(status="OBSERVATION_FAILED"),
        ]
        out = summarize_settlement_quality(rows)
        assert out.total == 4
        assert out.counts[GRADE_EXECUTABLE] == 2
        assert out.counts[GRADE_MARKET_CLOSE] == 1
        assert out.counts[GRADE_UNRESOLVED] == 1
        assert out.executable_rate == 0.5
        assert abs(sum(out.rates.values()) - 1.0) < 1e-9

    def test_every_grade_is_reported_even_at_zero(self):
        out = summarize_settlement_quality([_s(method=None)])
        assert set(out.counts) == {
            GRADE_EXECUTABLE,
            GRADE_MARKET_CLOSE,
            GRADE_INTRINSIC,
            GRADE_UNRESOLVED,
        }

    def test_an_empty_set_reports_zero_not_a_division_error(self):
        out = summarize_settlement_quality([])
        assert out.total == 0
        assert out.executable_rate == 0.0


class TestExecutableOnlyIsAFilterNotADeletion:
    def test_it_excludes_closing_mark_and_intrinsic_settlements(self):
        rows = [
            _s(method="EXECUTABLE_BID"),
            _s(method="EXECUTABLE_BID+MARKET_CLOSE_FALLBACK"),
            _s(method="EXPIRATION_INTRINSIC_AT_CLOSE"),
        ]
        assert len(executable_only(rows)) == 1

    def test_the_two_views_reconcile(self):
        """Executable-only is a strict subset, and the quality breakdown
        still describes the full set."""
        rows = [
            _s(method="EXECUTABLE_BID"),
            _s(method="EXECUTABLE_BID+MARKET_CLOSE_FALLBACK"),
            _s(status="OBSERVATION_FAILED"),
        ]
        subset = executable_only(rows)
        full = summarize_settlement_quality(rows)
        assert len(subset) == full.counts[GRADE_EXECUTABLE]
        assert len(subset) <= len(rows)
        assert all(r in rows for r in subset)

    def test_an_unresolved_row_is_never_executable(self):
        assert executable_only([_s(status="OBSERVATION_FAILED", method=None)]) == []
