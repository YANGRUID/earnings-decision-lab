"""V4.2 -- the bounded expiry ladder.

These pin the two things the audit actually established: that expiry choice
should be a COMPARISON rather than an index, and that the risk worth naming
is whether the contract still has life left at the moment V4 settles it.

Nothing here bans a short-dated expiry. A same-day expiry may well be the
right instrument; the tests assert that it is identified and characterised,
not that it is excluded.
"""

from datetime import date

import pytest

from analytics.decision.v4_2_expiry_ladder import (
    DEFAULT_MAX_VARIANTS,
    RISK_EXPIRES_ON_SETTLEMENT,
    RISK_EXPIRES_SAME_WEEK,
    RISK_STANDARD,
    LiquidityObservation,
    build_expiry_ladder,
    classify_settlement_risk,
    compare_expiry_variants,
    settlement_date_for,
    v4_1_selection,
)

# The real GWRE/ZS-era chain shape: a Thursday AMC report, a Friday weekly,
# then the following weeklies and the September monthly.
EARNINGS = date(2026, 9, 3)
DECISION = date(2026, 9, 3)
SETTLEMENT = date(2026, 9, 4)
CHAIN = {
    date(2026, 9, 3),   # expires ON the earnings date -- never eligible
    date(2026, 9, 4),   # expires ON the settlement date
    date(2026, 9, 11),
    date(2026, 9, 18),
    date(2026, 10, 16),
}


class TestEligibilityMatchesV41:
    def test_an_expiry_on_or_before_the_earnings_date_is_never_offered(self):
        ladder = build_expiry_ladder(
            CHAIN, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION,
        )
        assert date(2026, 9, 3) not in [v.expiration for v in ladder]

    def test_the_ladder_starts_where_v4_1_would_have_stopped(self):
        ladder = build_expiry_ladder(
            CHAIN, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION,
        )
        assert ladder[0].expiration == v4_1_selection(CHAIN, EARNINGS)
        assert ladder[0].ladder_position == 0


class TestBoundedFanOut:
    def test_the_ladder_is_bounded_so_contract_resolution_cannot_explode(self):
        ladder = build_expiry_ladder(
            CHAIN, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION,
        )
        assert len(ladder) == DEFAULT_MAX_VARIANTS

    def test_the_bound_is_configurable_and_respected(self):
        ladder = build_expiry_ladder(
            CHAIN, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION, max_variants=2,
        )
        assert len(ladder) == 2

    def test_a_thin_chain_yields_what_exists_without_padding(self):
        ladder = build_expiry_ladder(
            {date(2026, 9, 4)}, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION,
        )
        assert len(ladder) == 1

    def test_no_eligible_expiry_yields_an_empty_ladder_not_an_invented_one(self):
        assert build_expiry_ladder(
            {date(2026, 9, 1)}, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION,
        ) == []


class TestSettlementDayRisk:
    def test_an_expiry_on_the_settlement_date_is_named_as_such(self):
        assert classify_settlement_risk(SETTLEMENT, SETTLEMENT) == RISK_EXPIRES_ON_SETTLEMENT

    def test_an_expiry_before_settlement_is_also_the_highest_risk_class(self):
        assert classify_settlement_risk(date(2026, 9, 3), SETTLEMENT) == (
            RISK_EXPIRES_ON_SETTLEMENT
        )

    def test_a_nearby_expiry_is_distinguished_from_a_standard_one(self):
        assert classify_settlement_risk(date(2026, 9, 11), SETTLEMENT) == RISK_EXPIRES_SAME_WEEK
        assert classify_settlement_risk(date(2026, 10, 16), SETTLEMENT) == RISK_STANDARD

    def test_remaining_life_at_exit_is_what_gives_the_exit_a_two_sided_market(self):
        ladder = build_expiry_ladder(
            CHAIN, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION,
        )
        same_day = next(v for v in ladder if v.expiration == SETTLEMENT)
        later = next(v for v in ladder if v.expiration == date(2026, 9, 18))
        assert same_day.expires_on_settlement_date
        assert not same_day.has_remaining_life_at_exit
        assert later.has_remaining_life_at_exit
        assert later.dte_at_settlement == 14

    def test_a_same_day_expiry_is_offered_not_banned(self):
        """The audit's explicit instruction: compare, do not legislate a
        minimum DTE."""
        ladder = build_expiry_ladder(
            CHAIN, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION,
        )
        assert SETTLEMENT in [v.expiration for v in ladder]


class TestSettlementDateDerivation:
    def test_the_settlement_session_is_the_next_real_trading_day(self):
        sessions = [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 8)]
        assert settlement_date_for(date(2026, 9, 3), sessions) == date(2026, 9, 4)

    def test_a_weekend_or_holiday_is_skipped_because_sessions_are_supplied(self):
        """No weekday arithmetic and no invented calendar: a Friday report
        settles on the following Monday only because Monday is the next real
        session in the list."""
        sessions = [date(2026, 9, 4), date(2026, 9, 8)]  # Sep 7 is a holiday
        assert settlement_date_for(date(2026, 9, 4), sessions) == date(2026, 9, 8)

    def test_no_later_session_yields_none_rather_than_a_guess(self):
        assert settlement_date_for(date(2026, 9, 4), [date(2026, 9, 3)]) is None


class TestComparisonIsDiagnosticNotAWinner:
    def test_every_variant_is_reported_with_its_risk(self):
        ladder = build_expiry_ladder(
            CHAIN, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION,
        )
        rows = compare_expiry_variants(ladder)
        assert len(rows) == len(ladder)
        assert all("settlement_risk" in r for r in rows)

    def test_observed_liquidity_is_attached_where_it_exists_and_null_elsewhere(self):
        ladder = build_expiry_ladder(
            CHAIN, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
            decision_date=DECISION,
        )
        rows = compare_expiry_variants(
            ladder,
            {SETTLEMENT: LiquidityObservation(
                expiration=SETTLEMENT, legs_with_empty_bid=2, legs_observed=4
            )},
        )
        observed = next(r for r in rows if r["expiration"] == SETTLEMENT.isoformat())
        unobserved = next(r for r in rows if r["expiration"] == "2026-09-18")
        assert observed["empty_bid_legs"] == 2
        assert unobserved["empty_bid_legs"] is None, "absent observation must not read as zero"

    def test_the_comparison_returns_no_winner(self):
        """Choosing between expiries needs each variant's own modeled T+1
        economics, which requires candidate construction per expiry -- the
        part this phase deliberately does not ship."""
        rows = compare_expiry_variants(
            build_expiry_ladder(
                CHAIN, earnings_date=EARNINGS, settlement_date=SETTLEMENT,
                decision_date=DECISION,
            )
        )
        assert not any("selected" in r or "winner" in r for r in rows)

    @pytest.mark.parametrize("observed,expected", [((0, 4), 0.0), ((2, 4), 0.5), ((4, 4), 1.0)])
    def test_empty_bid_rate_is_a_real_measurement(self, observed, expected):
        empty, total = observed
        assert LiquidityObservation(
            expiration=SETTLEMENT, legs_with_empty_bid=empty, legs_observed=total
        ).empty_bid_rate == expected

    def test_empty_bid_rate_is_none_when_nothing_was_observed(self):
        assert LiquidityObservation(expiration=SETTLEMENT).empty_bid_rate is None
