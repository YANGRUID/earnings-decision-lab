"""Options Decision Engine V4.1 -- Decision/Entry Underlying-Price Drift
Diagnostic (2026-08-31).

The forensic audit's real, confirmed example: DY's real DecisionSnapshot
(id=4793) carried underlying_price=$380.95 from a VolatilitySnapshot
collected at 2026-08-25 17:15:29 UTC, while the real, live EntrySnapshot
captured moments later (same scheduler run) observed underlying_price=
$348.25 at 2026-08-25 19:55:25 UTC -- an 8.6% gap, traced to a real,
now-fixed source-coherence bug (see
services/options_reconstruction.py::resolve_best_actionable_option_market's
own ``force_live_refresh`` parameter and docstring).

This module is the read-only DIAGNOSTIC half of that fix -- it computes
and exposes the drift, it does not enforce any rejection threshold.
Per this task's own explicit instruction, no numeric cutoff is invented
here: an exhaustive check of this codebase (analytics/data_state.py,
services/options_analytics.py::compute_actionability) confirms no
established intraday market-data-age rule exists to anchor one to. A
future methodology decision, informed by real drift data this module
makes visible, is what should set an enforcement threshold -- not this
task.

Works from fields that already exist on real, already-persisted rows --
no new column, no schema change, no modification of any historical row.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class UnderlyingDriftObservation:
    decision_underlying_price: Decimal
    decision_underlying_observed_at: datetime | None
    entry_underlying_price: Decimal
    entry_underlying_observed_at: datetime | None
    drift_pct: Decimal
    drift_dollars: Decimal


def compute_underlying_drift(
    *,
    decision_underlying_price: Decimal,
    decision_underlying_observed_at: datetime | None,
    entry_underlying_price: Decimal,
    entry_underlying_observed_at: datetime | None,
) -> UnderlyingDriftObservation | None:
    """Returns None only when decision_underlying_price is exactly zero
    (drift_pct would be undefined) -- every other real input combination,
    including a missing decision_underlying_observed_at (e.g. HPQ, whose
    real option_snapshot_reference was genuinely NULL), still produces a
    real drift_pct; the timestamp fields are preserved as None rather
    than guessed, never backfilled."""
    if decision_underlying_price == 0:
        return None
    drift_dollars = abs(entry_underlying_price - decision_underlying_price)
    drift_pct = drift_dollars / decision_underlying_price
    return UnderlyingDriftObservation(
        decision_underlying_price=decision_underlying_price,
        decision_underlying_observed_at=decision_underlying_observed_at,
        entry_underlying_price=entry_underlying_price,
        entry_underlying_observed_at=entry_underlying_observed_at,
        drift_pct=drift_pct,
        drift_dollars=drift_dollars,
    )
