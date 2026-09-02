"""V4.1 methodology foundation (2026-08-31) -- capital semantics tests
(this task's own Section 22): two simultaneous standardized decisions
each independently use $2,000, and their aggregate losses must never be
presented as one $2,000 portfolio's drawdown."""

from decimal import Decimal

from analytics.decision.v4_capital import (
    PER_DECISION_CAPITAL,
    compute_standardized_decision_metrics,
    portfolio_simulation_available,
    summarize_standardized_cohort,
)


def test_per_decision_capital_is_the_real_v3_dollar_figure():
    assert PER_DECISION_CAPITAL == Decimal("2000")


def test_no_true_portfolio_simulator_exists():
    """The honest answer -- see this task's Section 10/22. If this ever
    flips to True, a real portfolio simulator (shared capital, entry/exit
    debits, concurrency accounting) must exist to justify it."""
    assert portfolio_simulation_available() is False


def test_return_on_standardized_capital_uses_the_fixed_2000_base():
    metrics = compute_standardized_decision_metrics(
        realized_pnl=Decimal("-810.00"),
        return_pct=Decimal("-150"),
        is_win=False,
        r_legacy=Decimal("-1.5"),
    )
    assert metrics.return_on_standardized_capital == Decimal("-810.00") / Decimal("2000")
    assert metrics.r_legacy == Decimal("-1.5")
    assert "T+1" not in metrics.r_legacy_caveat  # sanity: caveat is real prose, not a stub
    assert "expiration" in metrics.r_legacy_caveat.lower()


def test_two_simultaneous_decisions_each_independently_use_the_full_standardized_capital():
    """The exact scenario this task's Section 22 names: two decisions
    'simultaneously' open, each sized against its own $2,000 -- neither
    decision's standardized return is affected by the other one
    existing."""
    a = compute_standardized_decision_metrics(
        realized_pnl=Decimal("-1000"), return_pct=None, is_win=False, r_legacy=None
    )
    b = compute_standardized_decision_metrics(
        realized_pnl=Decimal("-1000"), return_pct=None, is_win=False, r_legacy=None
    )
    assert a.return_on_standardized_capital == Decimal("-0.5")
    assert b.return_on_standardized_capital == Decimal("-0.5")
    # Neither computation ever references the other decision or any
    # shared running balance -- each is a pure function of its own
    # realized_pnl and the fixed PER_DECISION_CAPITAL constant.


def test_cohort_summary_never_computes_a_portfolio_drawdown():
    metrics = [
        compute_standardized_decision_metrics(
            realized_pnl=Decimal("-1000"), return_pct=None, is_win=False, r_legacy=None
        ),
        compute_standardized_decision_metrics(
            realized_pnl=Decimal("-1500"), return_pct=None, is_win=False, r_legacy=None
        ),
    ]
    summary = summarize_standardized_cohort(metrics)

    assert summary.n == 2
    assert summary.losses == 2
    assert summary.total_realized_pnl == Decimal("-2500")
    # Aggregate real dollar loss (-2500) exceeds the standardized
    # per-decision capital (2000) -- exactly the shape that produced
    # V3's nonsensical >100% legacy figure. This summary must never
    # expose a drawdown/equity-curve field to compute a percentage from.
    assert not hasattr(summary, "max_drawdown")
    assert not hasattr(summary, "max_drawdown_pct")
    assert summary.portfolio_drawdown_available is False
    assert "not a valid portfolio drawdown" in summary.portfolio_drawdown_reason


def test_cohort_summary_handles_an_empty_cohort_honestly():
    summary = summarize_standardized_cohort([])
    assert summary.n == 0
    assert summary.mean_return_on_standardized_capital is None
    assert summary.median_return_on_standardized_capital is None
    assert summary.total_realized_pnl == Decimal(0)
