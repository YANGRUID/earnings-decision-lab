from datetime import UTC, datetime, timedelta

from analytics.research.freshness import (
    DataClass,
    FreshnessStatus,
    assess_freshness,
    needs_refresh,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def test_missing_when_never_updated():
    assert assess_freshness(DataClass.PRICE_HISTORY, None, NOW) == FreshnessStatus.MISSING


def test_fresh_within_policy_window():
    last_updated = NOW - timedelta(hours=1)
    assert assess_freshness(DataClass.OPTIONS_CHAIN, last_updated, NOW) == FreshnessStatus.FRESH


def test_stale_outside_policy_window():
    last_updated = NOW - timedelta(hours=10)
    assert assess_freshness(DataClass.OPTIONS_CHAIN, last_updated, NOW) == FreshnessStatus.STALE


def test_exactly_at_the_boundary_counts_as_fresh():
    from analytics.research.freshness import DEFAULT_POLICIES

    policy = DEFAULT_POLICIES[DataClass.EARNINGS_ESTIMATES]
    last_updated = NOW - policy.max_age
    assert (
        assess_freshness(DataClass.EARNINGS_ESTIMATES, last_updated, NOW) == FreshnessStatus.FRESH
    )


def test_every_data_class_has_a_default_policy():
    from analytics.research.freshness import DEFAULT_POLICIES

    for data_class in DataClass:
        assert data_class in DEFAULT_POLICIES


def test_custom_policies_are_actually_used_not_ignored():
    from analytics.research.freshness import FreshnessPolicy

    custom = {DataClass.PRICE_HISTORY: FreshnessPolicy(timedelta(days=9999), "test override")}
    last_updated = NOW - timedelta(days=30)
    # Would be STALE under the default policy, but the injected policy
    # accepts anything under ~27 years.
    assert (
        assess_freshness(DataClass.PRICE_HISTORY, last_updated, NOW, policies=custom)
        == FreshnessStatus.FRESH
    )


def test_needs_refresh_true_for_missing_and_stale():
    assert needs_refresh(FreshnessStatus.MISSING) is True
    assert needs_refresh(FreshnessStatus.STALE) is True


def test_needs_refresh_false_for_fresh():
    assert needs_refresh(FreshnessStatus.FRESH) is False
