"""Phase 4 methodology-experiments hardening (2026-08-26), Section 36 --
EXPERIMENTAL only. Never imported by the official ranking path."""

from datetime import date

from analytics.decision.experimental_dte_scoring import (
    EXPERIMENTAL_SWEET_SPOTS,
    experimental_dte_fit_score,
    strategy_family_for,
)
from analytics.options.strategy_candidates import StrategyCategory

EARNINGS_DATE = date(2026, 9, 16)


def test_every_real_strategy_category_maps_to_a_family():
    for category in StrategyCategory:
        assert strategy_family_for(category) in EXPERIMENTAL_SWEET_SPOTS


def test_long_vol_prefers_a_nearer_expiration_than_directional_debit():
    near = EARNINGS_DATE.replace(day=EARNINGS_DATE.day + 5)
    long_vol_score = experimental_dte_fit_score(StrategyCategory.LONG_STRADDLE, near, EARNINGS_DATE)
    directional_score = experimental_dte_fit_score(StrategyCategory.LONG_CALL, near, EARNINGS_DATE)
    # 5 days after earnings is inside long_vol's [1,10] sweet spot (full
    # score) but below directional_debit's [7,21] sweet spot (partial).
    assert long_vol_score > directional_score


def test_defined_risk_neutral_range_tolerates_a_later_expiration_than_long_vol():
    later = EARNINGS_DATE.replace(month=EARNINGS_DATE.month + 1, day=1)  # ~15 days out
    range_score = experimental_dte_fit_score(StrategyCategory.IRON_CONDOR, later, EARNINGS_DATE)
    long_vol_score = experimental_dte_fit_score(
        StrategyCategory.LONG_STRADDLE, later, EARNINGS_DATE
    )
    assert range_score > long_vol_score


def test_expiration_before_earnings_never_scores_favorably():
    before = date(2026, 9, 10)
    assert experimental_dte_fit_score(StrategyCategory.LONG_CALL, before, EARNINGS_DATE) == 0


def test_no_earnings_date_is_neutral_fraction():
    assert (
        experimental_dte_fit_score(
            StrategyCategory.LONG_CALL, date(2026, 9, 18), None, max_score=20
        )
        == 10
    )


def test_never_wired_into_official_ranking():
    """The explicit Section 36 requirement -- experimental only, not
    activated. If this ever starts failing, someone wired the
    experimental module into the official path without updating this
    guard, which is exactly what this test exists to catch."""
    import ast
    import inspect

    import analytics.decision.strategy_scoring as official_module

    tree = ast.parse(inspect.getsource(official_module))
    imported_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "experimental_dte_scoring" not in (official_module.__file__ or "")
    assert not any("experimental" in name.lower() for name in imported_names)
