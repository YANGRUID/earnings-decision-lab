"""Phase 4 methodology-experiments hardening (2026-08-26), Section 35 --
EXPERIMENTAL comparison only, never the official BenchmarkPortfolio
methodology."""

from datetime import date
from decimal import Decimal

from analytics.options.expiration_methodology_comparison import (
    compare_expiration_methodologies,
)
from analytics.options.expiration_selection import (
    ExpirationCandidate,
    ExpirationScoreBreakdown,
    ExpirationSelectionResult,
)

EARNINGS_DATE = date(2026, 9, 16)


def _candidate(expiration: date, total_score_parts: dict | None = None) -> ExpirationCandidate:
    parts = total_score_parts or dict(
        event_fit=20,
        liquidity=15,
        quote_coverage=15,
        bid_ask_quality=15,
        dte_suitability=15,
        data_quality=10,
    )
    return ExpirationCandidate(
        expiration=expiration,
        dte=(expiration - EARNINGS_DATE).days,
        days_after_earnings=(expiration - EARNINGS_DATE).days,
        contract_count=20,
        priceable_contract_count=18,
        quote_coverage=Decimal("0.9"),
        bid_ask_coverage=Decimal("0.9"),
        oi_coverage=Decimal("0.5"),
        volume_coverage=Decimal("0.5"),
        atm_iv=Decimal("0.4"),
        atm_spread_pct=Decimal("0.05"),
        quality="good",
        score=ExpirationScoreBreakdown(**parts),
        is_earnings_anchored=True,
    )


def test_legacy_and_scored_agree_when_nearest_is_also_best_scored():
    nearest = _candidate(date(2026, 9, 18))
    farther = _candidate(
        date(2026, 9, 25),
        dict(
            event_fit=15,
            liquidity=10,
            quote_coverage=10,
            bid_ask_quality=10,
            dte_suitability=10,
            data_quality=10,
        ),
    )
    result = ExpirationSelectionResult(
        mode="auto", selected=nearest, alternatives=[farther], reasons=[]
    )

    comparison = compare_expiration_methodologies(result, EARNINGS_DATE)

    assert comparison.legacy_expiration == date(2026, 9, 18)
    assert comparison.scored_expiration == date(2026, 9, 18)
    assert comparison.methodologies_agree is True
    assert comparison.legacy_candidate_evaluated_by_scored_engine is True
    assert comparison.scored_total_score == 90
    assert comparison.scored_liquidity == 15
    assert comparison.scored_dte_suitability == 15


def test_legacy_and_scored_disagree_when_scored_prefers_a_later_expiration():
    nearest = _candidate(
        date(2026, 9, 18),
        dict(
            event_fit=10,
            liquidity=5,
            quote_coverage=5,
            bid_ask_quality=5,
            dte_suitability=5,
            data_quality=5,
        ),
    )
    better_scored = _candidate(date(2026, 9, 25))
    result = ExpirationSelectionResult(
        mode="auto", selected=better_scored, alternatives=[nearest], reasons=[]
    )

    comparison = compare_expiration_methodologies(result, EARNINGS_DATE)

    assert comparison.legacy_expiration == date(2026, 9, 18)
    assert comparison.scored_expiration == date(2026, 9, 25)
    assert comparison.methodologies_agree is False


def test_no_earnings_date_means_no_legacy_candidate():
    candidate = _candidate(date(2026, 9, 18))
    result = ExpirationSelectionResult(mode="auto", selected=candidate, alternatives=[], reasons=[])

    comparison = compare_expiration_methodologies(result, None)

    assert comparison.legacy_expiration is None
    assert comparison.legacy_candidate_evaluated_by_scored_engine is False
    assert comparison.methodologies_agree is None
    assert comparison.scored_expiration == date(2026, 9, 18)


def test_no_selected_candidate_at_all():
    result = ExpirationSelectionResult(
        mode="auto", selected=None, alternatives=[], reasons=[], warning="no chain"
    )

    comparison = compare_expiration_methodologies(result, EARNINGS_DATE)

    assert comparison.scored_expiration is None
    assert comparison.legacy_expiration is None
    assert comparison.methodologies_agree is None
