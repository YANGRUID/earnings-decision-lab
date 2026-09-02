"""Options Decision Engine V4.1 -- Shadow Candidate Interface (2026-08-31).

INTERFACE ONLY. This module defines the data contract a future V4.5
shadow-candidate evaluation pipeline (forensic audit Part II Section 41)
would eventually populate -- it creates no database table, captures no
extra IBKR quote, and adds no scheduler load. Nothing in the official
pipeline constructs one of these today.

The problem this eventually solves: V3 (and V4, once it starts
generating real decisions) only ever captures its single selected
candidate. There is no way to know whether candidate #2 would have
performed better -- concretely demonstrated by the forensic audit's own
VEEV/HPQ walkthroughs, where VEEV's #1 pick was a real, decisive
14-point win over its runner-up, while HPQ's #1 beat its runner-up by
exactly one point, a real statistical coin flip whose alternative outcome
this project currently has no way to ever observe.
"""

from dataclasses import dataclass
from decimal import Decimal

from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory


@dataclass(frozen=True)
class ShadowCandidateEvaluation:
    """One non-official, evaluation-only record of a candidate that was
    NOT selected for a real decision. Never backed by portfolio capital,
    never a brokerage order, never any input to which candidate a real
    decision actually selects -- see this module's own docstring."""

    earnings_calendar_event_id: int
    engine_version: str
    candidate_rank: int
    strategy_category: StrategyCategory
    candidate: StrategyCandidate
    score: int
    score_breakdown: dict[str, int]
    methodology_version: str


@dataclass(frozen=True)
class ShadowEntryObservation:
    """A read-only, same-timestamp quote observation for one shadow
    candidate's own legs -- reuses the same warm-up/quote-telemetry
    machinery services/benchmark_entry_capture.py already has, but writes
    nowhere near the official EntrySnapshot table. NOT built in V4.1."""

    shadow_candidate_id: int
    observed_at_utc: str
    leg_prices: dict[str, Decimal]


@dataclass(frozen=True)
class ShadowExitObservation:
    """The settlement-time mirror of ShadowEntryObservation -- also not
    built in V4.1."""

    shadow_candidate_id: int
    observed_at_utc: str
    leg_prices: dict[str, Decimal]
    hypothetical_realized_pnl: Decimal
