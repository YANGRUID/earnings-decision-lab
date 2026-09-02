"""V4.2 market coherence policy foundation (2026-09-01) -- architecture
only, no enforcement (Section 20). Confirms the status representation is
correct and that nothing here can reject or alter a decision."""

from datetime import UTC, datetime
from decimal import Decimal

from analytics.decision.underlying_drift import compute_underlying_drift
from analytics.decision.v4_market_coherence import classify_market_coherence


def test_same_session_observations_classify_as_fresh():
    drift = compute_underlying_drift(
        decision_underlying_price=Decimal("100"),
        decision_underlying_observed_at=datetime(2026, 8, 25, 15, 0, tzinfo=UTC),
        entry_underlying_price=Decimal("100.50"),
        entry_underlying_observed_at=datetime(2026, 8, 25, 19, 55, tzinfo=UTC),
    )
    result = classify_market_coherence(
        drift=drift, live_refresh_attempted=True, live_refresh_succeeded=True
    )
    assert result.status == "fresh"


def test_the_real_dy_shape_classifies_as_fresh_same_session_just_hours_apart():
    """DY's own real timestamps both fall on 2026-08-25 -- V4.1's
    force_live_refresh fix addresses this at the source; this
    classification layer is a separate, informational status only."""
    drift = compute_underlying_drift(
        decision_underlying_price=Decimal("380.950000"),
        decision_underlying_observed_at=datetime(2026, 8, 25, 17, 15, 29, tzinfo=UTC),
        entry_underlying_price=Decimal("348.250000"),
        entry_underlying_observed_at=datetime(2026, 8, 25, 19, 55, 25, tzinfo=UTC),
    )
    result = classify_market_coherence(
        drift=drift, live_refresh_attempted=True, live_refresh_succeeded=True
    )
    assert result.status == "fresh"
    assert result.drift_pct is not None and result.drift_pct > Decimal("0.08")


def test_different_session_observations_classify_as_stale():
    drift = compute_underlying_drift(
        decision_underlying_price=Decimal("100"),
        decision_underlying_observed_at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
        entry_underlying_price=Decimal("100.50"),
        entry_underlying_observed_at=datetime(2026, 8, 25, 19, 55, tzinfo=UTC),
    )
    result = classify_market_coherence(
        drift=drift, live_refresh_attempted=True, live_refresh_succeeded=True
    )
    assert result.status == "stale"


def test_failed_live_refresh_is_its_own_status():
    drift = compute_underlying_drift(
        decision_underlying_price=Decimal("100"),
        decision_underlying_observed_at=None,
        entry_underlying_price=Decimal("100.50"),
        entry_underlying_observed_at=datetime(2026, 8, 25, 19, 55, tzinfo=UTC),
    )
    result = classify_market_coherence(
        drift=drift, live_refresh_attempted=True, live_refresh_succeeded=False
    )
    assert result.status == "live_refresh_failed"


def test_no_drift_observation_available_is_unknown_age():
    result = classify_market_coherence(
        drift=None, live_refresh_attempted=False, live_refresh_succeeded=None
    )
    assert result.status == "unknown_age"


def test_missing_decision_timestamp_is_unknown_age_not_guessed():
    """HPQ's real shape: no decision-time observation on record."""
    drift = compute_underlying_drift(
        decision_underlying_price=Decimal("28.58"),
        decision_underlying_observed_at=None,
        entry_underlying_price=Decimal("30.71"),
        entry_underlying_observed_at=datetime(2026, 8, 26, 19, 56, tzinfo=UTC),
    )
    result = classify_market_coherence(
        drift=drift, live_refresh_attempted=True, live_refresh_succeeded=True
    )
    assert result.status == "unknown_age"


def test_no_rejection_threshold_is_ever_applied():
    """This module has no notion of pass/fail -- confirmed structurally:
    MarketCoherenceResult carries no boolean 'accepted'/'rejected' field
    at all."""
    drift = compute_underlying_drift(
        decision_underlying_price=Decimal("100"),
        decision_underlying_observed_at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
        entry_underlying_price=Decimal("50"),  # a huge, 50% drift
        entry_underlying_observed_at=datetime(2026, 8, 25, 19, 55, tzinfo=UTC),
    )
    result = classify_market_coherence(
        drift=drift, live_refresh_attempted=True, live_refresh_succeeded=True
    )
    assert not hasattr(result, "accepted")
    assert not hasattr(result, "rejected")
    assert not hasattr(result, "is_valid")
    assert result.status == "stale"  # reported, never enforced
