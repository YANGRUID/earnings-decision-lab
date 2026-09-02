"""Read-only classification of a real EntryCaptureAttempt.capture_error
string into a named failure category (live market-data validation,
2026-08-26, Section 21).

Derived at READ time from the exact error text services/benchmark_entry_
capture.py already produces -- never a new stored column. EntryCaptureAttempt
is immutable (append-only, same reject_snapshot_update() trigger as every
other Phase 4 snapshot table; see its own module docstring), and this
task's own instruction prefers a derived read-side classification over a
schema change when one is possible. Categories match this task's Section
21 taxonomy exactly.

Keyed on the exact, real substrings benchmark_entry_capture.py and the
IBKR exception classes (providers/ibkr_client.py) actually produce --
verified against that source, not guessed:

    IBKRRateLimitedError        "... rate-limited the request to {path}"
    IBKRCompetingSessionError   "... (competing session) ..."
    IBKRNotAuthenticatedError   "... isn't authenticated ..."
    IBKRContractNotFoundError   "no listed underlying with an options
                                 section found for ..."
    IBKRGatewayUnavailableError "could not reach the IBKR Client Portal
                                 Gateway at ..." / "... request to {path}
                                 timed out"

Patterns are checked most-specific-first so a real rate-limit response
(wrapped as "options provider call failed: IBKR Client Portal Gateway
rate-limited the request to ...") is never mislabeled ENTRY_SYSTEM_ERROR,
and a real permission failure is never mislabeled ENTRY_QUOTE_UNAVAILABLE.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from models.enums import QuoteRequirement
from providers.ibkr_client import (
    IBKRCompetingSessionError,
    IBKRGatewayUnavailableError,
    IBKRNotAuthenticatedError,
    IBKRRateLimitedError,
)
from providers.ibkr_options import IBKRContractNotFoundError

if TYPE_CHECKING:
    from models.quote_acquisition_attempt import QuoteAcquisitionAttempt

EntryFailureCategory = Literal[
    "NO_ACTION",
    "ENTRY_RATE_LIMITED",
    "ENTRY_PERMISSION_ERROR",
    "ENTRY_CONTRACT_UNAVAILABLE",
    "ENTRY_STALE_QUOTE",
    "ENTRY_WINDOW_MISSED",
    "ENTRY_QUOTE_UNAVAILABLE",
    "ENTRY_SYSTEM_ERROR",
]

_PATTERNS: list[tuple[str, EntryFailureCategory]] = [
    ("no recommended strategy legs to enter", "NO_ACTION"),
    ("legs but no selected_expiration", "NO_ACTION"),
    ("rate-limited the request", "ENTRY_RATE_LIMITED"),
    ("competing session", "ENTRY_PERMISSION_ERROR"),
    ("isn't authenticated", "ENTRY_PERMISSION_ERROR"),
    ("no listed underlying with an options section found", "ENTRY_CONTRACT_UNAVAILABLE"),
    ("timestamp skew", "ENTRY_STALE_QUOTE"),
    ("past the permitted decision cutoff", "ENTRY_WINDOW_MISSED"),
    ("past the valid pre-earnings entry window", "ENTRY_WINDOW_MISSED"),
    ("before the valid pre-earnings entry window", "ENTRY_WINDOW_MISSED"),
    ("no ask quote available", "ENTRY_QUOTE_UNAVAILABLE"),
    ("no bid quote available", "ENTRY_QUOTE_UNAVAILABLE"),
    ("no live underlying quote available", "ENTRY_QUOTE_UNAVAILABLE"),
    ("no quote found for this contract", "ENTRY_QUOTE_UNAVAILABLE"),
    ("could not reach the IBKR Client Portal Gateway", "ENTRY_SYSTEM_ERROR"),
    ("Gateway request to", "ENTRY_SYSTEM_ERROR"),
]


def classify_entry_failure(capture_error: str | None) -> EntryFailureCategory | None:
    """``None`` only when there's genuinely no error text to classify (a
    CAPTURED or still-PENDING attempt). A real FAILED attempt whose text
    matches none of the known patterns above still gets an honest bucket
    (ENTRY_SYSTEM_ERROR) rather than ``None`` -- a silent "no
    classification" would be indistinguishable from "not a failure" to a
    caller building a dashboard off this."""
    if not capture_error:
        return None
    for pattern, category in _PATTERNS:
        if pattern in capture_error:
            return category
    return "ENTRY_SYSTEM_ERROR"


def requirement_met(row: "QuoteAcquisitionAttempt") -> bool:
    """Whether ``row`` actually satisfies its own ``required_side`` --
    public (Phase 4 quote-observability hardening, 2026-08-26, Section
    13) so services/quote_diagnostics.py's Operations read model can
    reuse the exact same rule this module's own classifier uses, instead
    of a second, driftable copy of it."""
    if row.required_side == QuoteRequirement.ASK:
        return row.ask_present
    if row.required_side == QuoteRequirement.BID:
        return row.bid_present
    if row.required_side == QuoteRequirement.BID_ASK:
        return row.bid_present and row.ask_present
    return row.last_present  # ANALYTICAL


def classify_from_structured_evidence(
    rows: list["QuoteAcquisitionAttempt"],
) -> EntryFailureCategory | None:
    """Derives a failure category directly from real, persisted
    QuoteAcquisitionAttempt rows for a capture attempt (IBKR execution-
    observability hardening, 2026-08-26, Section 12) -- preferred over
    free-text classification when structured telemetry actually exists.
    Returns ``None`` both when ``rows`` is empty (no structured telemetry
    at all -- e.g. a legacy/Aug 25 attempt, or a hard provider-call
    exception this pass's writers don't instrument -- see services/
    quote_telemetry.py's own docstring) AND when the structured evidence
    itself shows nothing wrong (every leg's required side was actually
    satisfied) -- in both cases the caller should fall back to
    ``classify_entry_failure`` for the real, free-text reason (e.g. a
    window-timing or stale-quote failure, which this table doesn't
    capture at all).

    ``rate_limited``/``permission_error``/``provider_error_category`` are
    now real, reachable signals (Phase 4 quote-observability hardening,
    2026-08-26, Section 10): a provider exception that aborts a capture
    before any quote resolves is persisted by services/quote_telemetry.py's
    persist_entry_exception_telemetry/persist_settlement_exception_
    telemetry, using services/entry_failure_taxonomy.py's own classify_
    provider_exception -- so a rate-limit, permission, or gateway failure
    on a FUTURE capture now shows up here directly, not only as free text
    on the parent capture attempt.
    """
    if not rows:
        return None
    if any(row.rate_limited for row in rows):
        return "ENTRY_RATE_LIMITED"
    if any(row.permission_error for row in rows):
        return "ENTRY_PERMISSION_ERROR"
    if any(not row.contract_resolved for row in rows):
        return "ENTRY_CONTRACT_UNAVAILABLE"
    # A gateway-level failure (unreachable/timed out) or any other
    # provider exception this classifier doesn't have a more specific
    # bucket for -- real connectivity trouble, not "no ask/bid ever
    # arrived," so it must never fall through to ENTRY_QUOTE_UNAVAILABLE
    # below.
    if any(
        row.provider_error_category in ("GATEWAY_TIMEOUT", "GATEWAY_UNREACHABLE", "UNCLASSIFIED")
        for row in rows
    ):
        return "ENTRY_SYSTEM_ERROR"
    final_rows = [row for row in rows if row.final_for_leg]
    if any(not requirement_met(row) for row in final_rows):
        return "ENTRY_QUOTE_UNAVAILABLE"
    return None


def classify_capture_failure(
    capture_error: str | None, telemetry_rows: list["QuoteAcquisitionAttempt"]
) -> EntryFailureCategory | None:
    """The one real entry point a caller (e.g. Operations) should use --
    structured evidence first, free-text fallback for legacy rows or
    hard provider exceptions with no structured telemetry (Section 12).
    Never rewrites ``capture_error`` itself; this is a pure, derived
    read, computed fresh every time it's needed."""
    structured = classify_from_structured_evidence(telemetry_rows)
    if structured is not None:
        return structured
    return classify_entry_failure(capture_error)


@dataclass(frozen=True)
class ProviderExceptionClassification:
    """Phase 4 quote-observability hardening (2026-08-26), Section 10 --
    what a real provider exception that aborted a capture BEFORE any
    quote resolved means for the structured QuoteAcquisitionAttempt rows
    services/quote_telemetry.py's exception-path writers persist, so a
    future capture failure is diagnosable from real, structured evidence
    instead of only EntryCaptureAttempt/SettlementCaptureAttempt's free-
    text ``capture_error``."""

    category: str
    rate_limited: bool = False
    permission_error: bool = False
    # False only for a real contract-resolution failure -- every other
    # category means the exception happened at/after resolution (priming
    # or polling), so whatever was requested was, as far as this
    # exception tells us, a real, resolvable contract.
    contract_resolved: bool = True


def classify_provider_exception(exc: Exception) -> ProviderExceptionClassification:
    """Keyed on the same real, typed IBKR exception classes ``classify_
    entry_failure``'s free-text patterns are keyed on the *messages* of
    (see this module's own docstring) -- checked by type here instead,
    since this function runs at the real except-block boundary where the
    exception object itself, not just its rendered string, is available.
    Order matters: IBKRNotAuthenticatedError/IBKRCompetingSessionError are
    both permission-shaped but distinct categories, checked before the
    more generic IBKRGatewayUnavailableError.
    """
    if isinstance(exc, IBKRRateLimitedError):
        return ProviderExceptionClassification(category="RATE_LIMITED", rate_limited=True)
    if isinstance(exc, IBKRCompetingSessionError):
        return ProviderExceptionClassification(category="PERMISSION_ERROR", permission_error=True)
    if isinstance(exc, IBKRNotAuthenticatedError):
        return ProviderExceptionClassification(category="AUTH_REQUIRED", permission_error=True)
    if isinstance(exc, IBKRContractNotFoundError):
        return ProviderExceptionClassification(
            category="CONTRACT_RESOLUTION_ERROR", contract_resolved=False
        )
    if isinstance(exc, IBKRGatewayUnavailableError):
        if "timed out" in str(exc):
            return ProviderExceptionClassification(category="GATEWAY_TIMEOUT")
        return ProviderExceptionClassification(category="GATEWAY_UNREACHABLE")
    return ProviderExceptionClassification(category="UNCLASSIFIED")
