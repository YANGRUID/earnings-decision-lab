"""A connected socket is not market-data readiness.

On 2026-10-13 IB Gateway lost its own link to IBKR at 17:06 UTC and never
reported it restored. For the next day and a half the LOCAL socket stayed up,
so ``ibkr.connected`` read True, pre-flight reported ready, and nothing in
Operations raised anything -- while two forward windows produced no decisions
and 42 settlements missed their legal window. The outage was visible the whole
time in the gateway's own status label; it was simply never read.

These tests pin the two places that now read it: a P0 alert while the loss
lasts, and a pre-flight check that names the action the operator has to take.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.operations import (
    IBKR_UPSTREAM_LOSS_GRACE,
    IBKR_UPSTREAM_REMEDY,
    PreflightCheck,
    detect_ibkr_upstream_alerts,
    get_preflight_readiness,
)

NOW = datetime(2026, 10, 14, 19, 0, tzinfo=UTC)


def _health(*, connected=True, lost_since=None, last_error=None):
    """Only the fields these two readers touch -- a full SystemHealth here
    would be fixture noise that hides which field decides the outcome."""
    return SimpleNamespace(
        ibkr=SimpleNamespace(
            connected=connected,
            upstream_lost_since=lost_since,
            last_error=last_error,
            state="degraded" if lost_since else "healthy",
        ),
        ai_provider=SimpleNamespace(
            configured=True,
            decision_view_config_error=None,
            decision_view_model="test",
            decision_view_thinking=True,
        ),
        scheduler=SimpleNamespace(running=True),
        v4_shadow=SimpleNamespace(enabled=True, note=None),
    )


_READINESS = SimpleNamespace(next_window_total=0, next_window_ready=0, next_window_at=None)


class TestTheUpstreamAlert:
    def test_a_live_upstream_raises_nothing(self):
        assert detect_ibkr_upstream_alerts(_health(), now=NOW) == []

    def test_a_loss_inside_the_grace_period_is_not_yet_critical(self):
        """A genuine blip is withdrawn within seconds (IBKR 1100 then
        1101/1102); alarming on one would train the operator to ignore this."""
        lost = NOW - IBKR_UPSTREAM_LOSS_GRACE + timedelta(seconds=30)
        assert detect_ibkr_upstream_alerts(_health(lost_since=lost), now=NOW) == []

    def test_a_sustained_loss_is_a_p0_alert_that_says_what_to_do(self):
        lost = NOW - timedelta(hours=26)
        alerts = detect_ibkr_upstream_alerts(_health(lost_since=lost), now=NOW)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.category == "ibkr_upstream_lost_critical"
        assert "CRITICAL (P0)" in alert.explanation
        assert "26.0h" in alert.explanation
        assert "2FA" in alert.explanation, "re-login is interactive; say so"
        assert alert.occurred_at == lost, "dated from the loss, not from now"

    def test_the_alert_fires_even_though_the_local_socket_is_connected(self):
        """The exact shape of the 2026-10-13 miss."""
        health = _health(connected=True, lost_since=NOW - timedelta(hours=3))
        assert detect_ibkr_upstream_alerts(health, now=NOW)


class TestPreflightReadsTheUpstream:
    def test_a_connected_socket_with_a_lost_upstream_is_not_ready(self, db_session):
        health = _health(connected=True, lost_since=NOW - timedelta(hours=3))
        result = get_preflight_readiness(db_session, health, _READINESS, [], now=NOW)
        check = next(c for c in result.checks if c.label.startswith("IBKR ready to observe"))
        assert check.passed is False
        assert check.detail == IBKR_UPSTREAM_REMEDY
        assert not result.ready
        assert "IBKR ready to observe the market" in result.blockers

    def test_a_healthy_connection_passes(self, db_session):
        health = _health(connected=True, lost_since=None)
        result = get_preflight_readiness(db_session, health, _READINESS, [], now=NOW)
        check = next(c for c in result.checks if c.label.startswith("IBKR ready to observe"))
        assert check.passed is True

    def test_positions_due_to_settle_get_their_own_check(self, db_session):
        """A missed settlement window can never be re-settled at the price it
        should have had -- only recovered from a closing mark. It deserves its
        own pre-flight line rather than riding on the decision window's."""
        due_at = NOW + timedelta(hours=20)
        pipeline = [
            SimpleNamespace(lifecycle_state="WAITING_SETTLEMENT", exit_timestamp=due_at),
            SimpleNamespace(lifecycle_state="WAITING_SETTLEMENT", exit_timestamp=due_at),
            SimpleNamespace(lifecycle_state="SETTLED", exit_timestamp=due_at),
        ]
        health = _health(connected=True, lost_since=NOW - timedelta(hours=3))
        result = get_preflight_readiness(
            db_session, health, _READINESS, [], pipeline=pipeline, now=NOW
        )
        check = next(c for c in result.checks if "due settlements" in c.label)
        assert check.passed is False
        assert "2 position(s) settle" in check.detail
        assert IBKR_UPSTREAM_REMEDY in check.detail

    def test_no_settlement_check_when_nothing_is_waiting(self, db_session):
        result = get_preflight_readiness(
            db_session, _health(), _READINESS, [], pipeline=[], now=NOW
        )
        assert not [c for c in result.checks if "due settlements" in c.label]


@pytest.mark.parametrize("passed", [True, False])
def test_preflight_check_is_a_plain_value(passed):
    check = PreflightCheck("x", passed, None)
    assert check.passed is passed
