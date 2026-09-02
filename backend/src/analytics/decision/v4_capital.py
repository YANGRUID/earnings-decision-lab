"""Options Decision Engine V4.1 -- Capital Semantics (2026-08-31).

The forensic audit's confirmed root cause of V3's nonsensical "460.8% of
peak equity" max-drawdown figure: BenchmarkPortfolio.cash_balance was
seeded at $2,000 and is never debited or credited by any code path in
this backend (confirmed by an exhaustive grep across every service file)
-- every one of V3's real entry-sizing checks independently treated it as
a fresh, undiminished per-trade budget, while
services/benchmark_track_record.py::compute_max_drawdown separately built
a genuine sequential equity curve against that same static base, as if
it really were one shared account. Those two assumptions are mutually
inconsistent; that inconsistency is the whole bug.

This module does not fix V3's historical number (Section 0: V3 settlement
math and historical rows are never touched). It defines the clean
methodology V4 uses instead, and gives V3's already-real, already-settled
observations an honest, correctly-labeled read-side reinterpretation.

TWO DELIBERATELY SEPARATE THINGS, NEVER CONFLATED (Section 10 of this
task):

  A. STANDARDIZED DECISION BENCHMARK -- each decision is graded
     independently against the same fixed reference capital
     (PER_DECISION_CAPITAL). This is what V3's real $2,000 sizing already
     functions as in practice; this module just gives it an honest name
     and stops pretending it is a shared account.

  B. PORTFOLIO SIMULATION -- a genuine shared-equity, capital-reserving,
     concurrency-aware simulation of a real account trading many
     positions at once. THIS DOES NOT EXIST ANYWHERE IN THIS CODEBASE
     TODAY. portfolio_simulation_available() below returns False, and
     every function in this module that could be mistaken for computing
     a portfolio-level statistic (drawdown, equity curve) refuses to,
     loudly, rather than silently producing V3's same misleading number
     under a new name.
"""

from dataclasses import dataclass
from decimal import Decimal

PER_DECISION_CAPITAL = Decimal("2000")
"""Standardized reference capital every decision is independently graded
against -- NOT a shared portfolio balance. Two concurrently-open
decisions each use the full $2,000; that is by design, so strategy
quality can be compared consistently across events regardless of how
many other decisions happen to be open at the same time. Deliberately
matches V3's real BenchmarkPortfolio.initial_capital/cash_balance value
-- this is the same number, finally given an honest name."""

CAPITAL_SEMANTICS_VERSION = "standardized_per_decision_v1"


def portfolio_simulation_available() -> bool:
    """The honest answer, always. No code path in this backend reserves
    capital across concurrently-open positions, debits/credits a shared
    balance on entry/exit, or reconstructs a true multi-position equity
    curve -- see this module's own docstring for the exhaustive grep
    that confirmed BenchmarkPortfolio.cash_balance is never mutated.
    Callers must check this before showing anything labeled "portfolio"
    drawdown/equity for V4's standardized-decision data; V4.1 does not
    build that simulator (it is out of scope -- see V4_ROADMAP)."""
    return False


@dataclass(frozen=True)
class StandardizedDecisionMetrics:
    """Metrics for exactly ONE decision, graded against its own
    independent PER_DECISION_CAPITAL -- never aggregated into a shared
    equity curve. ``r_legacy_caveat`` is carried alongside V3's existing
    R (never replaced -- Section 11 of this task explicitly defers a new
    R formula) so any UI showing it can render the caveat next to it."""

    realized_pnl: Decimal
    return_on_standardized_capital: Decimal
    return_on_entry_cash: Decimal | None
    is_win: bool | None
    r_legacy: Decimal | None
    r_legacy_caveat: str


R_LEGACY_CAVEAT = (
    "V3's R uses the position's theoretical expiration max-risk as its denominator, but "
    "settlement occurs one trading day later at real bid/ask, not at expiration -- realized "
    "loss can and does legitimately exceed the theoretical max (forensic audit Part I "
    "Section 9). Not presented as a clean normalized loss metric without this caveat."
)


def compute_standardized_decision_metrics(
    *,
    realized_pnl: Decimal,
    return_pct: Decimal | None,
    is_win: bool | None,
    r_legacy: Decimal | None,
) -> StandardizedDecisionMetrics:
    """Pure, read-only computation over an ALREADY-SETTLED decision's own
    real, already-persisted settlement figures -- never recomputes or
    alters realized_pnl/return_pct/r_multiple themselves (those remain
    exactly settlement_math.py's own numbers, Section 0/11)."""
    return StandardizedDecisionMetrics(
        realized_pnl=realized_pnl,
        return_on_standardized_capital=realized_pnl / PER_DECISION_CAPITAL,
        return_on_entry_cash=(return_pct / Decimal(100)) if return_pct is not None else None,
        is_win=is_win,
        r_legacy=r_legacy,
        r_legacy_caveat=R_LEGACY_CAVEAT,
    )


@dataclass(frozen=True)
class StandardizedCohortSummary:
    """An aggregate over MANY independent standardized decisions --
    win/loss counts, mean/median return-on-standardized-capital -- but
    deliberately NO drawdown and NO equity curve. See
    portfolio_simulation_available()."""

    n: int
    wins: int
    losses: int
    mean_return_on_standardized_capital: Decimal | None
    median_return_on_standardized_capital: Decimal | None
    total_realized_pnl: Decimal
    portfolio_drawdown_available: bool
    portfolio_drawdown_reason: str


PORTFOLIO_DRAWDOWN_UNAVAILABLE_REASON = (
    "No true portfolio simulator exists in this codebase (no shared capital reservation, no "
    "concurrency accounting, no cash debit/credit on entry/exit). Each decision below used its "
    "own independent $2,000 standardized capital; summing their real dollar losses against one "
    "shared $2,000 base, the way V3's legacy figure does, is not a valid portfolio drawdown. "
    "See 'V3 Legacy Aggregate Loss' for the historical number, correctly re-labeled instead of "
    "hidden."
)


def summarize_standardized_cohort(
    metrics: list[StandardizedDecisionMetrics],
) -> StandardizedCohortSummary:
    """Aggregates independent per-decision metrics -- explicitly refuses
    to produce a portfolio-level drawdown/equity-curve figure, exactly
    the behavior Section 22 of this task requires: if a true portfolio
    simulator doesn't exist, say so, rather than silently computing one
    under a new name."""
    n = len(metrics)
    wins = sum(1 for m in metrics if m.is_win is True)
    losses = sum(1 for m in metrics if m.is_win is False)
    returns = [m.return_on_standardized_capital for m in metrics]
    total_pnl = sum((m.realized_pnl for m in metrics), Decimal(0))
    mean_return = (sum(returns, Decimal(0)) / len(returns)) if returns else None
    median_return = _median(returns) if returns else None
    return StandardizedCohortSummary(
        n=n,
        wins=wins,
        losses=losses,
        mean_return_on_standardized_capital=mean_return,
        median_return_on_standardized_capital=median_return,
        total_realized_pnl=total_pnl,
        portfolio_drawdown_available=portfolio_simulation_available(),
        portfolio_drawdown_reason=PORTFOLIO_DRAWDOWN_UNAVAILABLE_REASON,
    )


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
