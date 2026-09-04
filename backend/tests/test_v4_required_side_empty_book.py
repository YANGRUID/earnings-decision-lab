"""Regression cover for the 2026-09-04 V4 required-side settlement
incident.

Twenty-seven configurations across five companies failed to settle at the
15:30 ET window with "required exit side missing". The live wire evidence
(captured in-process on the shared production connection) showed IBKR was
answering every one of those requests: ``tickPrice(66=DELAYED_BID) = -1``
paired with ``tickSize(69=DELAYED_BID_SIZE) = 0``, while the ask side
quoted normally. Control contracts on the same delayed feed returned real
bids of 0.65 and 0.01 with real sizes, so -1/0 is IBKR's explicit "no order
is bid on this side", not silence.

The defect was that normalization collapsed that explicit statement into
"no tick arrived": the bounded warm-up then spent all five attempts (a
measured 7.54s per leg) re-asking an answered question, and the failure was
recorded under a category that says the quote never came.

These tests pin the distinction that fix rests on:
  * an empty book is RECORDED, not discarded, and never becomes a price;
  * an empty book TERMINATES the warm-up, a genuinely absent tick does not;
  * the persisted failure says NO_BID/NO_ASK vs REQUIRED_SIDE_TIMEOUT;
  * no substitution is ever introduced for the missing side.
"""

from models.enums import QuoteRequirement
from providers.ibkr_tws_client import TWSConnectionManager
from providers.ibkr_tws_options import _book_empty, _requirement_satisfied, _requirement_terminal


def _manager() -> TWSConnectionManager:
    return TWSConnectionManager(host="host.docker.internal", port=4002, client_id=101)


def _pending(manager, req_id=5):
    pending = manager._register(req_id, "test")  # noqa: SLF001
    pending.result = {}
    return pending


class TestEmptyBookIsRecordedNotDiscarded:
    def test_minus_one_bid_records_the_sentinel_and_never_a_price(self):
        manager = _manager()
        manager.nextValidId(1)
        pending = _pending(manager)
        manager.marketDataType(5, 3)
        manager.tickPrice(5, 66, -1.0, None)  # DELAYED_BID, the real wire value
        manager.tickSize(5, 69, 0)  # DELAYED_BID_SIZE
        assert "bid" not in pending.result, "a -1 sentinel must never become a price"
        assert pending.result["bid_no_data_sentinel"] is True
        assert pending.result["bid_size"] == 0

    def test_minus_one_ask_is_recorded_symmetrically(self):
        manager = _manager()
        manager.nextValidId(1)
        pending = _pending(manager)
        manager.tickPrice(5, 2, -1.0, None)
        manager.tickSize(5, 3, 0)
        assert "ask" not in pending.result
        assert pending.result["ask_no_data_sentinel"] is True

    def test_a_real_penny_bid_is_still_a_real_bid(self):
        """The live control: 0.01 with size 4 is a genuine quote and must
        never be mistaken for an empty book."""
        manager = _manager()
        manager.nextValidId(1)
        pending = _pending(manager)
        manager.tickPrice(5, 66, 0.01, None)
        manager.tickSize(5, 69, 4)
        assert pending.result["bid"] == 0.01
        assert "bid_no_data_sentinel" not in pending.result
        assert _book_empty(pending.result, "bid") is None

    def test_negative_last_and_close_stay_discarded_without_a_book_claim(self):
        manager = _manager()
        manager.nextValidId(1)
        pending = _pending(manager)
        manager.tickPrice(5, 68, -1.0, None)  # DELAYED_LAST
        manager.tickPrice(5, 75, -1.0, None)  # DELAYED_CLOSE
        assert pending.result == {}


class TestBookEmptyClassification:
    def test_sentinel_with_zero_size_is_an_empty_book(self):
        assert _book_empty({"bid_no_data_sentinel": True, "bid_size": 0}, "bid") is True

    def test_sentinel_with_no_size_tick_is_still_an_empty_book(self):
        assert _book_empty({"bid_no_data_sentinel": True}, "bid") is True

    def test_silence_is_not_an_empty_book(self):
        assert _book_empty({}, "bid") is None

    def test_sentinel_contradicted_by_a_real_size_is_not_claimed_empty(self):
        assert _book_empty({"bid_no_data_sentinel": True, "bid_size": 12}, "bid") is False


class TestWarmUpTermination:
    def test_empty_bid_book_terminates_a_bid_requirement(self):
        result = {"bid_no_data_sentinel": True, "bid_size": 0, "ask": 0.05}
        assert _requirement_satisfied(result, QuoteRequirement.BID) is False
        assert _requirement_terminal(result, QuoteRequirement.BID) is True

    def test_an_absent_bid_tick_does_not_terminate(self):
        """The case that MUST still spend its bounded retries: nothing was
        said about the bid at all."""
        result = {"ask": 0.05}
        assert _requirement_satisfied(result, QuoteRequirement.BID) is False
        assert _requirement_terminal(result, QuoteRequirement.BID) is False

    def test_last_arriving_first_neither_satisfies_nor_terminates_a_bid_requirement(self):
        result = {"last": 0.01, "close": 3.79}
        assert _requirement_satisfied(result, QuoteRequirement.BID) is False
        assert _requirement_terminal(result, QuoteRequirement.BID) is False

    def test_greeks_arriving_first_neither_satisfy_nor_terminate(self):
        result = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": -0.0, "implied_volatility": 1.6}
        assert _requirement_satisfied(result, QuoteRequirement.BID) is False
        assert _requirement_terminal(result, QuoteRequirement.BID) is False

    def test_an_empty_ask_book_does_not_terminate_a_bid_requirement(self):
        result = {"ask_no_data_sentinel": True, "ask_size": 0}
        assert _requirement_terminal(result, QuoteRequirement.BID) is False

    def test_empty_ask_book_terminates_an_ask_requirement(self):
        result = {"ask_no_data_sentinel": True, "ask_size": 0, "bid": 1.10}
        assert _requirement_satisfied(result, QuoteRequirement.ASK) is False
        assert _requirement_terminal(result, QuoteRequirement.ASK) is True

    def test_a_real_delayed_bid_satisfies_the_requirement(self):
        assert _requirement_satisfied({"bid": 0.65}, QuoteRequirement.BID) is True


class TestBoundedRetryStillApplies:
    """The warm-up must stop early ONLY on a definitive answer -- silence
    still gets every attempt it is entitled to."""

    def _loop(self, manager, results, requirement):
        seen: list[int] = []

        def satisfied(result):
            return _requirement_satisfied(result, requirement)

        def terminal(result):
            return _requirement_terminal(result, requirement)

        step = iter(results)

        def fake_snapshot(contract, generic_ticks="", timeout=None):
            value = next(step, results[-1])
            seen.append(1)
            return value

        manager.request_market_data_snapshot = fake_snapshot  # type: ignore[method-assign]
        manager.request_market_data_with_requirement(
            contract=object(),
            requirement_satisfied=satisfied,
            requirement_terminal=terminal,
            generic_ticks="",
            retry_delay=0.0,
        )
        return len(seen)

    def test_silence_exhausts_all_five_attempts(self):
        assert self._loop(_manager(), [{"ask": 0.05}], QuoteRequirement.BID) == 5

    def test_an_empty_book_stops_after_the_first_attempt(self):
        attempts = self._loop(
            _manager(),
            [{"bid_no_data_sentinel": True, "bid_size": 0, "ask": 0.05}],
            QuoteRequirement.BID,
        )
        assert attempts == 1, "IBKR already answered -- retrying cannot change it"

    def test_a_bid_arriving_on_a_later_attempt_still_wins(self):
        attempts = self._loop(
            _manager(),
            [{"ask": 0.05}, {"ask": 0.05}, {"bid": 0.20, "ask": 0.05}],
            QuoteRequirement.BID,
        )
        assert attempts == 3


class TestExitFailureTaxonomy:
    def _row(self, index, side, state, **extra):
        row = {
            "leg_index": index,
            "action": "buy" if side == "bid" else "sell",
            "right": "call",
            "strike": "127.000000",
            "external_contract_id": "904384049",
            "required_side": side,
            "required_side_state": state,
            "bid": None,
            "ask": "0.01",
            "last": "0.01",
            "bid_size": 0,
            "ask_size": 173,
            "market_data_quality": "delayed",
        }
        row.update(extra)
        return row

    def test_an_empty_bid_book_is_reported_as_no_bid(self):
        from services.v4_shadow_cohort import EXIT_NO_BID, _exit_failure_category

        assert _exit_failure_category([self._row(0, "bid", "book_empty")]) == EXIT_NO_BID

    def test_an_empty_ask_book_is_reported_as_no_ask(self):
        from services.v4_shadow_cohort import EXIT_NO_ASK, _exit_failure_category

        assert _exit_failure_category([self._row(1, "ask", "book_empty")]) == EXIT_NO_ASK

    def test_a_quote_that_never_arrived_is_a_timeout_not_a_no_bid(self):
        from services.v4_shadow_cohort import EXIT_REQUIRED_SIDE_TIMEOUT, _exit_failure_category

        assert (
            _exit_failure_category([self._row(0, "bid", "unavailable")])
            == EXIT_REQUIRED_SIDE_TIMEOUT
        )

    def test_a_mixed_failure_stays_under_the_generic_category(self):
        from services.v4_shadow_cohort import EXIT_REQUIRED_SIDE_MISSING, _exit_failure_category

        rows = [self._row(0, "bid", "book_empty"), self._row(3, "bid", "unavailable")]
        assert _exit_failure_category(rows) == EXIT_REQUIRED_SIDE_MISSING

    def test_the_detail_names_the_side_the_contract_and_what_was_seen(self):
        from services.v4_shadow_cohort import _exit_failure_detail

        detail = _exit_failure_detail([self._row(0, "bid", "book_empty")])
        for fragment in ("leg 0", "conId 904384049", "BID", "book_empty", "ask=0.01", "delayed"):
            assert fragment in detail
        assert "no midpoint, last-price, historical or intrinsic substitution" in detail


class TestNoSubstitutionEverAppears:
    def test_an_empty_book_never_produces_a_price_field(self):
        """The whole point: recognising the empty book must not quietly
        become a zero, a last price, or a close."""
        manager = _manager()
        manager.nextValidId(1)
        pending = _pending(manager)
        manager.tickPrice(5, 66, -1.0, None)
        manager.tickSize(5, 69, 0)
        manager.tickPrice(5, 68, 0.01, None)  # DELAYED_LAST
        manager.tickPrice(5, 75, 3.79, None)  # DELAYED_CLOSE
        assert "bid" not in pending.result
        assert pending.result["last"] == 0.01
        assert pending.result["close"] == 3.79
        assert _requirement_satisfied(pending.result, QuoteRequirement.BID) is False
