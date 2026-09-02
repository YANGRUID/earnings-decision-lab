"""Options Decision Engine V4.3.1 -- Target-Aware Chain Coverage
(2026-09-03).

Answers ONE question this task exists to fix: when a V4.3 strike
target isn't found in the chain data we have, is that because the
target genuinely doesn't exist in the real listed chain, or only
because the chain DATA WE HAVE is a narrow window that never asked
about strikes that far out?

V4.3's own historical replay used ``options_snapshot`` rows collected
around a persisted ``STRIKES_AROUND_ATM=5`` window (see
providers/ibkr_options.py). This task's own Section 2 audit (real
source re-read, not assumed from documentation) confirmed:

    ``GET /iserver/secdef/strikes`` already returns the COMPLETE
    listed strike array for a given underlying+month in ONE response
    -- the request carries no strike-bounding parameter at all.
    ``STRIKES_AROUND_ATM`` is applied as a pure client-side Python
    list slice (``all_strikes[low:high]``, providers/ibkr_options.py
    line ~633) AFTER that complete response is already received and
    parsed. It is this codebase's OWN post-receipt trimming choice,
    not a limitation of what the provider's discovery endpoint
    returns per request.

That means V3.'s DG replay result
(UNCONSTRUCTABLE_NO_PROTECTIVE_WING_AVAILABLE, V4.3's own report
Section N) proved only that the PERSISTED, narrow captured window was
insufficient -- never that IBKR's real Aug-25 listed chain lacked a
strike that far out. This module gives V4 the vocabulary to say that
honestly, instead of collapsing both cases into one ambiguous
"unconstructable."

TWO DISTINCT EPISTEMIC SITUATIONS, never conflated:

  1. LIVE/CURRENT: full strike metadata CAN be (re-)fetched today via
     the same real ``/iserver/secdef/strikes`` call, just without the
     client-side slice -- see ``fetch_complete strike list`` in
     providers/ibkr_options.py's own docstring reasoning; V4.3.1 does
     not modify that file (Section 1), it only adds the vocabulary to
     describe what a caller who DID fetch the complete list learned.

  2. HISTORICAL: the past is past. A decision made on 2026-08-25 only
     ever had whatever was captured/frozen for it at that time. No
     live re-fetch today is a legitimate stand-in for "what IBKR's
     real Aug-25 chain contained" -- re-checking today's chain and
     presenting it as if it answers that question would be exactly
     the mistake this task's own Section 21 forbids ("Do NOT
     reinterpret historical facts using current chain data"). Historical
     replay therefore has its OWN dedicated status,
     CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW, which is never resolved by
     any amount of live data, no matter how complete.

Pure, deterministic classification -- no DB access, no live HTTP call.
Callers supply whatever ChainMetadata they have (a narrow captured
window, a complete live fetch, or none at all) and get back an honest
status, never a guess.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from analytics.decision.v4_strike_resolver import Right

ChainMetadataSource = Literal["complete_listed", "captured_window", "synthetic", "unknown"]

CoverageStatus = Literal[
    "TARGET_RESOLVED",
    "TARGET_BEYOND_CAPTURED_WINDOW",
    "TARGET_NOT_LISTED",
    "NO_PROTECTIVE_WING_LISTED",
    "QUOTE_UNAVAILABLE",
    "CONTRACT_RESOLUTION_ERROR",
    "CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW",
]

# Reason-code-shaped constants mirroring the CoverageStatus values --
# kept as real module constants (not just the Literal) so callers can
# import and compare without retyping the string, matching this
# project's own established convention (v4_compatibility.py,
# v4_strike_engine.py).
TARGET_RESOLVED = "TARGET_RESOLVED"
TARGET_BEYOND_CAPTURED_WINDOW = "TARGET_BEYOND_CAPTURED_WINDOW"
TARGET_NOT_LISTED = "TARGET_NOT_LISTED"
NO_PROTECTIVE_WING_LISTED = "NO_PROTECTIVE_WING_LISTED"
QUOTE_UNAVAILABLE = "QUOTE_UNAVAILABLE"
CONTRACT_RESOLUTION_ERROR = "CONTRACT_RESOLUTION_ERROR"
CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW = "CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW"


@dataclass(frozen=True)
class ChainMetadata:
    """Whatever strike metadata a caller actually has for one
    ticker+expiration -- deliberately separate from quotes/prices
    (Section 3: "broad strike METADATA discovery, narrow market-data
    requests"). ``source`` is the single field every coverage
    judgment below turns on; it is never inferred or guessed by this
    module -- the caller states it, honestly, based on how the data
    was actually obtained.

    - "complete_listed": every real strike returned by an unsliced
      ``/iserver/secdef/strikes`` call (or equivalent complete
      discovery) -- confirms real absence when a target falls outside
      this set.
    - "captured_window": a real but bounded/pre-sliced set (e.g. the
      persisted ATM+/-5 snapshot) -- a target falling outside this
      set is ambiguous, not confirmed absent.
    - "synthetic": test/diagnostic data with no real provenance claim
      at all.
    - "unknown": provenance genuinely not tracked -- treated the same
      as "captured_window" (the conservative, non-overclaiming
      choice) rather than "complete_listed".
    """

    ticker: str
    expiration: date
    observed_at: datetime
    call_strikes: tuple[Decimal, ...]
    put_strikes: tuple[Decimal, ...]
    source: ChainMetadataSource
    captured_window_size: int | None = None


@dataclass(frozen=True)
class CoverageAssessment:
    target_price: Decimal
    right: Right
    status: CoverageStatus
    nearest_listed_strike: Decimal | None
    metadata_source: ChainMetadataSource
    reason: str


def _strikes_for(metadata: ChainMetadata, right: Right) -> tuple[Decimal, ...]:
    return metadata.call_strikes if right == "call" else metadata.put_strikes


def assess_target_coverage(
    target_price: Decimal, right: Right, metadata: ChainMetadata
) -> CoverageAssessment:
    """Classifies ONE target against ONE metadata set. Never claims
    ``TARGET_NOT_LISTED`` (a real, confirmed absence) unless
    ``metadata.source == "complete_listed"`` -- every other source
    that can't cover the target degrades honestly to
    ``TARGET_BEYOND_CAPTURED_WINDOW`` instead (Section 4's own
    explicit instruction: "do not classify 'not present in the
    persisted snapshot' as 'does not exist in the real option
    chain'")."""
    strikes = _strikes_for(metadata, right)
    if not strikes:
        if metadata.source == "complete_listed":
            return CoverageAssessment(
                target_price=target_price,
                right=right,
                status="TARGET_NOT_LISTED",
                nearest_listed_strike=None,
                metadata_source=metadata.source,
                reason=(
                    f"Complete listed-strike metadata for {metadata.ticker} "
                    f"{metadata.expiration} carries no real {right} strikes at all."
                ),
            )
        return CoverageAssessment(
            target_price=target_price,
            right=right,
            status="TARGET_BEYOND_CAPTURED_WINDOW",
            nearest_listed_strike=None,
            metadata_source=metadata.source,
            reason=f"No {right} strike metadata available from source={metadata.source!r}.",
        )

    nearest = min(strikes, key=lambda s: abs(s - target_price))
    within_range = min(strikes) <= target_price <= max(strikes)
    if within_range:
        return CoverageAssessment(
            target_price=target_price,
            right=right,
            status="TARGET_RESOLVED",
            nearest_listed_strike=nearest,
            metadata_source=metadata.source,
            reason=f"Target resolves to real listed strike {nearest} within the covered range.",
        )

    if metadata.source == "complete_listed":
        return CoverageAssessment(
            target_price=target_price,
            right=right,
            status="TARGET_NOT_LISTED",
            nearest_listed_strike=nearest,
            metadata_source=metadata.source,
            reason=(
                f"Target {target_price} lies beyond the real listed range "
                f"[{min(strikes)}, {max(strikes)}] -- confirmed against complete chain "
                "metadata, a genuine boundary of the real listed chain, not a captured-"
                "window artifact."
            ),
        )

    return CoverageAssessment(
        target_price=target_price,
        right=right,
        status="TARGET_BEYOND_CAPTURED_WINDOW",
        nearest_listed_strike=nearest,
        metadata_source=metadata.source,
        reason=(
            f"Target {target_price} lies beyond the captured window "
            f"[{min(strikes)}, {max(strikes)}] (source={metadata.source!r}) -- this does "
            "NOT confirm the real listed chain lacks a strike that far out, only that "
            "this window never asked."
        ),
    )


def assess_protective_wing_coverage(
    short_strike: Decimal, right: Right, direction: Literal["up", "down"], metadata: ChainMetadata
) -> CoverageAssessment:
    """Same judgment as ``assess_target_coverage``, specialized for
    "is there a real strike further out than the short strike" (the
    DG-shaped question, Section 21). A wing target IS the short strike
    itself, offset one step beyond in ``direction`` -- so this reuses
    the SAME complete-vs-window distinction, just reports
    ``NO_PROTECTIVE_WING_LISTED`` (confirmed) instead of
    ``TARGET_NOT_LISTED`` when metadata is complete and nothing exists
    beyond the short strike."""
    strikes = sorted(_strikes_for(metadata, right))
    beyond = [s for s in strikes if (s > short_strike if direction == "up" else s < short_strike)]
    if beyond:
        wing = min(beyond) if direction == "up" else max(beyond)
        return CoverageAssessment(
            target_price=short_strike,
            right=right,
            status="TARGET_RESOLVED",
            nearest_listed_strike=wing,
            metadata_source=metadata.source,
            reason=f"Real protective wing found at {wing}.",
        )
    if metadata.source == "complete_listed":
        return CoverageAssessment(
            target_price=short_strike,
            right=right,
            status="NO_PROTECTIVE_WING_LISTED",
            nearest_listed_strike=None,
            metadata_source=metadata.source,
            reason=(
                f"No real {right} strike exists beyond {short_strike} in the complete "
                "listed chain -- a confirmed real boundary, not a captured-window gap."
            ),
        )
    return CoverageAssessment(
        target_price=short_strike,
        right=right,
        status="TARGET_BEYOND_CAPTURED_WINDOW",
        nearest_listed_strike=None,
        metadata_source=metadata.source,
        reason=(
            f"No {right} strike beyond {short_strike} within the captured window "
            f"(source={metadata.source!r}) -- does not confirm the real chain lacks one."
        ),
    )


def historical_replay_status(assessment: CoverageAssessment) -> CoverageStatus:
    """The one, deliberate translation for backward-looking analysis
    (Section 5/21): whatever a live-context assessment would call
    ambiguous (TARGET_BEYOND_CAPTURED_WINDOW / NO_PROTECTIVE_WING_LISTED
    from anything other than complete_listed metadata) becomes the
    historical-specific CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW instead
    -- because for a past decision, no live re-fetch can ever upgrade
    "captured_window" to "complete_listed" after the fact. A genuinely
    "complete_listed" historical assessment (which never occurs for
    this project's real data today, since nothing captures a full
    historical chain) would still pass through as TARGET_NOT_LISTED/
    NO_PROTECTIVE_WING_LISTED unchanged -- this function does not
    fabricate ambiguity where real confirmed data exists."""
    if assessment.metadata_source == "complete_listed":
        return assessment.status
    if assessment.status in ("TARGET_BEYOND_CAPTURED_WINDOW", "NO_PROTECTIVE_WING_LISTED"):
        return "CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW"
    return assessment.status
