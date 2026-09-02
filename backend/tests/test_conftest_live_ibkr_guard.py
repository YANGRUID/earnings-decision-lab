"""IBKR TWS Migration, post-cutover cleanup A8 -- proves the live-socket
guard in tests/conftest.py actually fires.

Without this, the guard is untestable safety theater: it would look
correct, block nothing if it silently stopped applying (a rename, an
import move, a fixture-ordering change), and nobody would find out until
a test suite dialed the real brokerage Gateway again.
"""

import os

import pytest
from tests.conftest import ALLOW_LIVE_IBKR_ENV_VAR

from providers.ibkr_tws_client import TWSConnectionManager


class TestLiveIbkrSocketGuard:
    def test_a_real_connect_attempt_is_refused_loudly(self):
        """The guard must raise, not silently no-op -- a test that tries
        to reach a live Gateway has a real bug worth surfacing."""
        manager = TWSConnectionManager(host="127.0.0.1", port=4001, client_id=999)
        with pytest.raises(AssertionError, match="REAL socket connection"):
            manager.connect("127.0.0.1", 4001, 999)

    def test_the_refusal_explains_how_to_opt_in_deliberately(self):
        manager = TWSConnectionManager(host="127.0.0.1", port=4001, client_id=999)
        with pytest.raises(AssertionError, match=ALLOW_LIVE_IBKR_ENV_VAR):
            manager.connect("127.0.0.1", 4001, 999)

    def test_guard_is_active_by_default_in_this_suite(self):
        """The opt-out must be off unless someone deliberately set it --
        if this ever fails, the whole suite is free to dial production."""
        assert os.environ.get(ALLOW_LIVE_IBKR_ENV_VAR) != "1"

    def test_connect_and_start_cannot_reach_a_socket_either(self):
        """The guard sits on the one call every real connection funnels
        through, so the public entrypoint is covered too -- not merely the
        low-level method."""
        manager = TWSConnectionManager(host="127.0.0.1", port=4001, client_id=999)
        with pytest.raises(AssertionError, match="REAL socket connection"):
            manager.connect_and_start()
