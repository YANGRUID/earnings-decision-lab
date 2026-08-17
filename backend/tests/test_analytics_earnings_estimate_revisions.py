from decimal import Decimal

from analytics.earnings.estimate_revisions import (
    eps_revision_direction,
    revenue_revision_direction,
)
from models.enums import RevisionDirection


class TestEpsRevisionDirection:
    def test_more_up_than_down_is_up(self):
        assert eps_revision_direction(23, 0) == RevisionDirection.UP

    def test_more_down_than_up_is_down(self):
        assert eps_revision_direction(1, 5) == RevisionDirection.DOWN

    def test_equal_nonzero_counts_is_flat(self):
        assert eps_revision_direction(3, 3) == RevisionDirection.FLAT

    def test_both_zero_is_flat(self):
        assert eps_revision_direction(0, 0) == RevisionDirection.FLAT

    def test_both_none_is_unknown(self):
        assert eps_revision_direction(None, None) == RevisionDirection.UNKNOWN

    def test_one_none_treated_as_zero(self):
        # Alpha Vantage returns null (not 0) for a trailing-7-day count with
        # no revisions in that specific window -- real observed shape.
        assert eps_revision_direction(5, None) == RevisionDirection.UP
        assert eps_revision_direction(None, 5) == RevisionDirection.DOWN


class TestRevenueRevisionDirection:
    def test_higher_current_is_up(self):
        assert (
            revenue_revision_direction(Decimal("129741678360"), Decimal("122598000000"))
            == RevisionDirection.UP
        )

    def test_lower_current_is_down(self):
        assert (
            revenue_revision_direction(Decimal("100"), Decimal("200")) == RevisionDirection.DOWN
        )

    def test_equal_is_flat(self):
        assert revenue_revision_direction(Decimal("100"), Decimal("100")) == RevisionDirection.FLAT

    def test_no_previous_snapshot_is_unknown(self):
        assert revenue_revision_direction(Decimal("100"), None) == RevisionDirection.UNKNOWN

    def test_no_current_value_is_unknown(self):
        assert revenue_revision_direction(None, Decimal("100")) == RevisionDirection.UNKNOWN
