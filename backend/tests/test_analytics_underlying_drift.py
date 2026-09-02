"""V4.1 methodology foundation (2026-08-31), Section 14/16 -- the
decision/entry underlying-price drift diagnostic, regression-tested
against the real DY failure mode: DecisionSnapshot.underlying_price
$380.95 (from a VolatilitySnapshot collected 2026-08-25 17:15:29 UTC)
vs. the real, live EntryCaptureAttempt.underlying_price $348.25
(observed 2026-08-25 19:55:25 UTC) -- an 8.6% gap."""

from datetime import UTC, datetime
from decimal import Decimal

from analytics.decision.underlying_drift import compute_underlying_drift


def test_dy_failure_mode_produces_the_real_observed_8_point_6_percent_drift():
    observation = compute_underlying_drift(
        decision_underlying_price=Decimal("380.950000"),
        decision_underlying_observed_at=datetime(2026, 8, 25, 17, 15, 29, tzinfo=UTC),
        entry_underlying_price=Decimal("348.250000"),
        entry_underlying_observed_at=datetime(2026, 8, 25, 19, 55, 25, tzinfo=UTC),
    )
    assert observation is not None
    assert observation.drift_dollars == Decimal("32.700000")
    # Matches the real, independently-computed forensic-audit figure to
    # within float/Decimal rounding.
    assert round(observation.drift_pct, 4) == round(Decimal("32.70") / Decimal("380.95"), 4)
    assert observation.drift_pct > Decimal("0.08")


def test_zero_drift_when_prices_agree():
    observation = compute_underlying_drift(
        decision_underlying_price=Decimal("100"),
        decision_underlying_observed_at=None,
        entry_underlying_price=Decimal("100"),
        entry_underlying_observed_at=None,
    )
    assert observation is not None
    assert observation.drift_pct == Decimal(0)
    assert observation.drift_dollars == Decimal(0)


def test_missing_decision_observed_at_is_preserved_as_none_never_guessed():
    """HPQ's real shape: option_snapshot_reference was genuinely NULL --
    the timestamp must stay None, never backfilled or approximated."""
    observation = compute_underlying_drift(
        decision_underlying_price=Decimal("28.58"),
        decision_underlying_observed_at=None,
        entry_underlying_price=Decimal("30.71"),
        entry_underlying_observed_at=datetime(2026, 8, 26, 19, 56, 28, tzinfo=UTC),
    )
    assert observation is not None
    assert observation.decision_underlying_observed_at is None
    assert observation.entry_underlying_observed_at is not None
    assert observation.drift_pct > 0


def test_zero_decision_price_returns_none_rather_than_dividing_by_zero():
    observation = compute_underlying_drift(
        decision_underlying_price=Decimal(0),
        decision_underlying_observed_at=None,
        entry_underlying_price=Decimal("10"),
        entry_underlying_observed_at=None,
    )
    assert observation is None


def test_no_enforcement_threshold_is_applied_a_large_drift_is_still_just_reported():
    """This module computes and exposes drift -- it never rejects,
    raises, or refuses on any magnitude (this task's own Section 15:
    enforcement threshold is explicitly deferred to a later methodology
    decision)."""
    observation = compute_underlying_drift(
        decision_underlying_price=Decimal("100"),
        decision_underlying_observed_at=None,
        entry_underlying_price=Decimal("50"),
        entry_underlying_observed_at=None,
    )
    assert observation is not None
    assert observation.drift_pct == Decimal("0.5")
