"""Live market-data validation (2026-08-26), Section 21 -- validates
classify_entry_failure against the exact real message shapes
services/benchmark_entry_capture.py and providers/ibkr_client.py's own
exception classes actually produce (not synthetic fake-provider test
strings, which exist only to test that a failure is honestly recorded,
not what it's classified as)."""

from models.enums import QuoteAcquisitionCaptureType, QuoteRequirement
from models.quote_acquisition_attempt import QuoteAcquisitionAttempt
from providers.ibkr_client import (
    IBKRCompetingSessionError,
    IBKRGatewayUnavailableError,
    IBKRNotAuthenticatedError,
    IBKRRateLimitedError,
)
from providers.ibkr_options import IBKRContractNotFoundError
from services.entry_failure_taxonomy import (
    classify_capture_failure,
    classify_entry_failure,
    classify_from_structured_evidence,
    classify_provider_exception,
)


def test_none_for_no_error_text():
    assert classify_entry_failure(None) is None
    assert classify_entry_failure("") is None


def test_no_recommended_legs_is_no_action():
    assert (
        classify_entry_failure("decision_snapshot has no recommended strategy legs to enter")
        == "NO_ACTION"
    )


def test_missing_selected_expiration_is_no_action():
    assert (
        classify_entry_failure("decision_snapshot has legs but no selected_expiration")
        == "NO_ACTION"
    )


def test_real_rate_limited_error_is_never_mislabeled_no_ask():
    exc = IBKRRateLimitedError("IBKR Client Portal Gateway rate-limited the request to /x")
    wrapped = f"options provider call failed: {exc}"
    assert classify_entry_failure(wrapped) == "ENTRY_RATE_LIMITED"


def test_real_competing_session_error_is_permission_error():
    exc = IBKRCompetingSessionError(
        "another brokerage session is currently active for this IBKR account "
        "(competing session) -- only one session can hold the connection at a time"
    )
    wrapped = f"options provider call failed: {exc}"
    assert classify_entry_failure(wrapped) == "ENTRY_PERMISSION_ERROR"


def test_real_not_authenticated_error_is_never_mislabeled_quote_unavailable():
    exc = IBKRNotAuthenticatedError(
        "IBKR Client Portal Gateway is reachable but the brokerage session isn't "
        "authenticated -- log in at the Gateway's own web page, then retry"
    )
    wrapped = f"options provider call failed: {exc}"
    assert classify_entry_failure(wrapped) == "ENTRY_PERMISSION_ERROR"


def test_real_contract_not_found_error_is_contract_unavailable():
    exc = IBKRContractNotFoundError(
        "no listed underlying with an options section found for 'ZZFAKE'"
    )
    wrapped = f"options provider call failed: {exc}"
    assert classify_entry_failure(wrapped) == "ENTRY_CONTRACT_UNAVAILABLE"


def test_real_gateway_unavailable_error_is_system_error():
    exc = IBKRGatewayUnavailableError(
        "could not reach the IBKR Client Portal Gateway at https://localhost:5001/v1/api "
        "-- is it running?"
    )
    wrapped = f"options provider call failed: {exc}"
    assert classify_entry_failure(wrapped) == "ENTRY_SYSTEM_ERROR"


def test_real_gateway_timeout_error_is_system_error():
    exc = IBKRGatewayUnavailableError(
        "IBKR Client Portal Gateway request to /iserver/marketdata/snapshot timed out"
    )
    wrapped = f"options provider call failed: {exc}"
    assert classify_entry_failure(wrapped) == "ENTRY_SYSTEM_ERROR"


def test_timestamp_skew_is_stale_quote():
    msg = (
        "underlying/option quote timestamp skew (0:12:00) exceeds 0:05:00 -- refusing to "
        "combine a stale underlying observation with a fresh option quote in one official entry"
    )
    assert classify_entry_failure(msg) == "ENTRY_STALE_QUOTE"


def test_decision_generated_after_cutoff_is_window_missed():
    msg = (
        "decision_snapshot.generated_at (2026-08-26T20:05:00+00:00) is past the permitted "
        "decision cutoff (2026-08-26T19:55:00+00:00 + 0:05:00) -- refusing to back an official "
        "entry with a decision that may not honestly predate the earnings reaction"
    )
    assert classify_entry_failure(msg) == "ENTRY_WINDOW_MISSED"


def test_capture_too_late_is_window_missed():
    msg = (
        "capture time (2026-08-26T23:00:00+00:00) is past the valid pre-earnings entry window "
        "(2026-08-26T19:55:00+00:00 + 0:05:00) -- refusing to silently backfill an entry after "
        "the market may have already reacted"
    )
    assert classify_entry_failure(msg) == "ENTRY_WINDOW_MISSED"


def test_capture_too_early_is_window_missed():
    msg = (
        "capture time (2026-08-26T14:00:00+00:00) is before the valid pre-earnings entry window "
        "(2026-08-26T19:55:00+00:00 - 0:05:00) -- refusing to capture a benchmark entry "
        "materially earlier than the scheduled entry, which would not represent the same "
        "market moment"
    )
    assert classify_entry_failure(msg) == "ENTRY_WINDOW_MISSED"


def test_no_ask_for_long_leg_is_quote_unavailable():
    assert classify_entry_failure("no ask quote available for a long leg") == (
        "ENTRY_QUOTE_UNAVAILABLE"
    )


def test_no_bid_for_short_leg_is_quote_unavailable():
    assert classify_entry_failure("no bid quote available for a short leg") == (
        "ENTRY_QUOTE_UNAVAILABLE"
    )


def test_no_live_underlying_quote_is_quote_unavailable():
    assert (
        classify_entry_failure("no live underlying quote available from the options provider")
        == "ENTRY_QUOTE_UNAVAILABLE"
    )


def test_no_quote_found_for_contract_is_quote_unavailable():
    assert classify_entry_failure("no quote found for this contract") == "ENTRY_QUOTE_UNAVAILABLE"


def test_unrecognized_real_error_falls_back_to_system_error_not_none():
    assert classify_entry_failure("something genuinely unexpected happened") == (
        "ENTRY_SYSTEM_ERROR"
    )


def test_rate_limit_wins_even_if_message_also_mentions_quote():
    """Ordering check: a real rate-limit failure that happens to occur
    while resolving a quote must still classify as rate-limited, not as
    a generic quote-unavailable failure."""
    msg = "options provider call failed: IBKR Client Portal Gateway rate-limited the request to /iserver/marketdata/snapshot while resolving a quote"  # noqa: E501
    assert classify_entry_failure(msg) == "ENTRY_RATE_LIMITED"


def _row(**overrides) -> QuoteAcquisitionAttempt:
    defaults = dict(
        capture_attempt_type=QuoteAcquisitionCaptureType.ENTRY,
        entry_capture_attempt_id=1,
        ticker="TEST",
        required_side=QuoteRequirement.ASK,
        snapshot_attempt_number=1,
        elapsed_ms=1000,
        bid_present=False,
        ask_present=False,
        last_present=True,
        rate_limited=False,
        permission_error=False,
        contract_resolved=True,
        final_for_leg=True,
    )
    defaults.update(overrides)
    return QuoteAcquisitionAttempt(**defaults)


class TestClassifyFromStructuredEvidence:
    """IBKR execution-observability hardening (2026-08-26), Section 12 --
    structured telemetry, when it exists, must be preferred over
    free-text classification."""

    def test_no_rows_is_none(self):
        assert classify_from_structured_evidence([]) is None

    def test_required_side_never_satisfied_is_quote_unavailable(self):
        rows = [
            _row(snapshot_attempt_number=1, ask_present=False, final_for_leg=False),
            _row(snapshot_attempt_number=2, ask_present=False, final_for_leg=True),
        ]
        assert classify_from_structured_evidence(rows) == "ENTRY_QUOTE_UNAVAILABLE"

    def test_required_side_eventually_satisfied_is_none(self):
        """A real success -- no failure to classify at all."""
        rows = [
            _row(snapshot_attempt_number=1, ask_present=False, final_for_leg=False),
            _row(snapshot_attempt_number=2, ask_present=True, final_for_leg=True),
        ]
        assert classify_from_structured_evidence(rows) is None

    def test_unresolved_contract_is_contract_unavailable(self):
        rows = [_row(contract_resolved=False, snapshot_attempt_number=0, ask_present=False)]
        assert classify_from_structured_evidence(rows) == "ENTRY_CONTRACT_UNAVAILABLE"

    def test_rate_limited_flag_wins_over_quote_unavailable(self):
        rows = [_row(rate_limited=True, ask_present=False)]
        assert classify_from_structured_evidence(rows) == "ENTRY_RATE_LIMITED"

    def test_permission_error_flag_wins_over_quote_unavailable(self):
        rows = [_row(permission_error=True, ask_present=False)]
        assert classify_from_structured_evidence(rows) == "ENTRY_PERMISSION_ERROR"

    def test_bid_requirement_checked_for_a_sell_leg(self):
        rows = [_row(required_side=QuoteRequirement.BID, bid_present=False, ask_present=True)]
        assert classify_from_structured_evidence(rows) == "ENTRY_QUOTE_UNAVAILABLE"

    def test_only_final_rows_count_toward_the_verdict(self):
        """An early, incomplete poll must never itself be read as the
        final outcome -- only the row(s) marked final_for_leg matter."""
        rows = [
            _row(snapshot_attempt_number=1, ask_present=False, final_for_leg=False),
            _row(snapshot_attempt_number=2, ask_present=False, final_for_leg=False),
            _row(snapshot_attempt_number=3, ask_present=True, final_for_leg=True),
        ]
        assert classify_from_structured_evidence(rows) is None


class TestClassifyCaptureFailure:
    """The one real combined entry point -- structured first, free-text
    fallback for legacy rows or hard provider exceptions."""

    def test_prefers_structured_evidence_when_present(self):
        rows = [_row(ask_present=False)]
        result = classify_capture_failure("some unrelated free text", rows)
        assert result == "ENTRY_QUOTE_UNAVAILABLE"

    def test_falls_back_to_free_text_when_no_structured_rows(self):
        """The real Aug 25 case -- no QuoteAcquisitionAttempt rows exist
        for any legacy attempt, so the existing free-text classifier is
        exactly what still applies, unchanged."""
        result = classify_capture_failure("no ask quote available for a long leg", [])
        assert result == "ENTRY_QUOTE_UNAVAILABLE"

    def test_falls_back_to_free_text_when_structured_evidence_shows_no_problem(self):
        """Structured rows exist but show success (e.g. a later,
        unrelated failure like a missed window) -- falls back to the
        real free-text reason rather than reporting None."""
        rows = [_row(ask_present=True)]
        result = classify_capture_failure(
            "capture time (...) is past the valid pre-earnings entry window (...)", rows
        )
        assert result == "ENTRY_WINDOW_MISSED"


class TestClassifyProviderException:
    """Phase 4 quote-observability hardening (2026-08-26), Section 10 --
    keyed on the exact same real, typed IBKR exception classes used
    above, checked by type (not message pattern) at the real except-block
    boundary."""

    def test_rate_limited_error(self):
        exc = IBKRRateLimitedError("IBKR Client Portal Gateway rate-limited the request to /x")
        result = classify_provider_exception(exc)
        assert result.category == "RATE_LIMITED"
        assert result.rate_limited is True
        assert result.permission_error is False
        assert result.contract_resolved is True

    def test_competing_session_error_is_permission_error(self):
        exc = IBKRCompetingSessionError("another brokerage session is currently active")
        result = classify_provider_exception(exc)
        assert result.category == "PERMISSION_ERROR"
        assert result.permission_error is True
        assert result.rate_limited is False

    def test_not_authenticated_error_is_auth_required(self):
        exc = IBKRNotAuthenticatedError("the brokerage session isn't authenticated")
        result = classify_provider_exception(exc)
        assert result.category == "AUTH_REQUIRED"
        assert result.permission_error is True

    def test_contract_not_found_error_marks_contract_unresolved(self):
        exc = IBKRContractNotFoundError("no listed underlying with an options section found")
        result = classify_provider_exception(exc)
        assert result.category == "CONTRACT_RESOLUTION_ERROR"
        assert result.contract_resolved is False
        assert result.rate_limited is False
        assert result.permission_error is False

    def test_gateway_timeout_message_is_gateway_timeout(self):
        exc = IBKRGatewayUnavailableError(
            "IBKR Client Portal Gateway request to /iserver/marketdata/snapshot timed out"
        )
        result = classify_provider_exception(exc)
        assert result.category == "GATEWAY_TIMEOUT"
        assert result.contract_resolved is True

    def test_gateway_unreachable_message_is_gateway_unreachable(self):
        exc = IBKRGatewayUnavailableError(
            "could not reach the IBKR Client Portal Gateway at https://localhost:5001/v1/api"
        )
        result = classify_provider_exception(exc)
        assert result.category == "GATEWAY_UNREACHABLE"

    def test_unrecognized_exception_is_unclassified(self):
        result = classify_provider_exception(ValueError("something genuinely unexpected"))
        assert result.category == "UNCLASSIFIED"
        assert result.rate_limited is False
        assert result.permission_error is False
        assert result.contract_resolved is True


class TestStructuredEvidenceCoversExceptionCategories:
    """The exception-path categories that aren't RATE_LIMITED/PERMISSION_
    ERROR/unresolved-contract must still classify as a real failure, not
    fall through to ENTRY_QUOTE_UNAVAILABLE (a gateway outage is not "no
    ask/bid ever arrived")."""

    def test_gateway_timeout_is_entry_system_error(self):
        rows = [_row(provider_error_category="GATEWAY_TIMEOUT", ask_present=False)]
        assert classify_from_structured_evidence(rows) == "ENTRY_SYSTEM_ERROR"

    def test_gateway_unreachable_is_entry_system_error(self):
        rows = [_row(provider_error_category="GATEWAY_UNREACHABLE", ask_present=False)]
        assert classify_from_structured_evidence(rows) == "ENTRY_SYSTEM_ERROR"

    def test_unclassified_provider_exception_is_entry_system_error(self):
        rows = [_row(provider_error_category="UNCLASSIFIED", ask_present=False)]
        assert classify_from_structured_evidence(rows) == "ENTRY_SYSTEM_ERROR"
