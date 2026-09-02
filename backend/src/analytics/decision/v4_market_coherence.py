"""Options Decision Engine V4.2 -- Market Coherence Policy Foundation
(2026-09-01).

V4.1 fixed the DY stale-snapshot fast path with
``resolve_best_actionable_option_market``'s ``force_live_refresh``
parameter. But even with that fix, its own fallback path can still
legitimately reuse persisted (previous-session) market data when a live
refresh genuinely fails -- correct, honest behavior (see that function's
own CASE 2/3 docstring), not a bug. V3 is NOT changed here (this task's
Section 20 is explicit: "For V3: do NOT change this behavior in V4.2").

This module gives V4 the ARCHITECTURAL ABILITY to represent and
eventually reason about that fallback outcome explicitly -- a
``MarketCoherenceStatus``/``MarketCoherenceResult`` a future V4 stage
could use to decide whether to trust a decision's own market context.
No numeric drift threshold is chosen here (this task's own Section 20:
"Do not choose an arbitrary percentage threshold yet") -- consistent
with V4.1's ``underlying_drift.py``, which found no established
intraday market-data-age rule anywhere in this codebase to anchor one
to.

V4.2 DOES NOT ACTIVATE ANY REJECTION. Nothing here blocks, filters, or
degrades a real decision -- this is a pure data/status representation,
exercised only by tests and the read-only replay/diagnostic layer.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from analytics.decision.underlying_drift import UnderlyingDriftObservation

MarketCoherenceStatus = Literal["fresh", "stale", "live_refresh_failed", "unknown_age"]


@dataclass(frozen=True)
class MarketCoherenceResult:
    """The status this task's Section 20 asks for, plus the real
    underlying observation it was derived from. ``status`` is
    informational only in V4.2 -- nothing consumes it to reject or alter
    a decision."""

    status: MarketCoherenceStatus
    decision_underlying_price: Decimal
    decision_underlying_observed_at: datetime | None
    entry_underlying_price: Decimal | None
    entry_underlying_observed_at: datetime | None
    drift_pct: Decimal | None
    live_refresh_attempted: bool
    live_refresh_succeeded: bool | None
    reason: str


def classify_market_coherence(
    *,
    drift: UnderlyingDriftObservation | None,
    live_refresh_attempted: bool,
    live_refresh_succeeded: bool | None,
) -> MarketCoherenceResult:
    """Pure classification, no enforcement. ``live_refresh_succeeded`` is
    None when no live refresh was ever attempted (e.g. market closed, or
    a caller that never sets force_live_refresh at all)."""
    if drift is None:
        status: MarketCoherenceStatus = "unknown_age"
        reason = "No decision/entry underlying-price pair was available to compare."
    elif live_refresh_attempted and live_refresh_succeeded is False:
        status = "live_refresh_failed"
        reason = (
            "A live refresh was attempted (force_live_refresh=True) but failed, falling back "
            "to the previous-session-close path -- a real, honestly-labeled fallback, not an "
            "error, but market context may be from a completed prior session."
        )
    elif drift.decision_underlying_observed_at is not None and (
        drift.entry_underlying_observed_at is not None
        and drift.decision_underlying_observed_at.date()
        == drift.entry_underlying_observed_at.date()
    ):
        status = "fresh"
        reason = "Decision and entry underlying observations fall on the same real session."
    elif drift.decision_underlying_observed_at is None:
        status = "unknown_age"
        reason = "No real decision-time observation timestamp is on record for this decision."
    else:
        status = "stale"
        reason = "Decision and entry underlying observations fall on different real sessions."

    return MarketCoherenceResult(
        status=status,
        decision_underlying_price=drift.decision_underlying_price if drift else Decimal(0),
        decision_underlying_observed_at=drift.decision_underlying_observed_at if drift else None,
        entry_underlying_price=drift.entry_underlying_price if drift else None,
        entry_underlying_observed_at=drift.entry_underlying_observed_at if drift else None,
        drift_pct=drift.drift_pct if drift else None,
        live_refresh_attempted=live_refresh_attempted,
        live_refresh_succeeded=live_refresh_succeeded,
        reason=reason,
    )
