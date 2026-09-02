"""Phase 4 methodology-experiments hardening (2026-08-26), Section 35 --
EXPERIMENTAL comparison of LEGACY AUTO (nearest listed expiration after
earnings, analytics/options/implied_move.py::select_expiration_after --
what the official BenchmarkPortfolio's Auto mode actually uses) against
SCORED AUTO (the existing 6-factor Expiration Engine, services/
expiration_engine.py::resolve_auto_expiration).

Read-only, no live provider call of its own: takes an already-computed
``ExpirationSelectionResult`` (from a real call the caller already made,
e.g. the existing GET /{symbol}/expirations diagnostic endpoint) and
derives which of its own already-discovered candidates the legacy rule
would have picked -- zero extra provider round-trips, since the legacy
rule (min expiration after earnings_date) never needs anything the
scored engine's own discovery didn't already fetch.

Never changes official methodology: this module is never called from
services/decision_engine.py, services/decision_pipeline.py, or services/
decision_snapshot_freezing.py -- see this phase's own final report for
why (Section 35: "Do NOT silently change the current official benchmark
methodology in this pass... Do not activate SCORED AUTO for the official
BenchmarkPortfolio without explicit approval after comparison data
exists").
"""

from dataclasses import dataclass
from datetime import date

from analytics.options.expiration_selection import ExpirationCandidate, ExpirationSelectionResult
from analytics.options.implied_move import select_expiration_after


@dataclass(frozen=True)
class ExpirationMethodologyComparison:
    legacy_expiration: date | None
    legacy_dte: int | None
    scored_expiration: date | None
    scored_dte: int | None
    scored_total_score: int | None
    scored_liquidity: int | None
    scored_quote_coverage: int | None
    scored_bid_ask_quality: int | None
    scored_dte_suitability: int | None
    scored_data_quality: int | None
    # True when the legacy candidate wasn't among the candidates the
    # scored engine actually discovered/evaluated (bounded to its own
    # max_candidates) -- an honest limitation flag, never silently
    # treated as "methodologies agree" or "disagree" in that case.
    legacy_candidate_evaluated_by_scored_engine: bool
    methodologies_agree: bool | None


def compare_expiration_methodologies(
    result: ExpirationSelectionResult, earnings_date: date | None
) -> ExpirationMethodologyComparison:
    """``result`` must be a real ``resolve_auto_expiration(...)`` result
    (mode="auto") -- comparing against a manual-mode result would compare
    the legacy rule against a human's override, not against the scored
    engine's own pick, which isn't this comparison's purpose."""
    candidates: list[ExpirationCandidate] = (
        [result.selected] if result.selected is not None else []
    ) + list(result.alternatives)

    legacy_candidate: ExpirationCandidate | None = None
    if earnings_date is not None and candidates:
        legacy_expiration = select_expiration_after(
            {c.expiration for c in candidates}, earnings_date
        )
        if legacy_expiration is not None:
            legacy_candidate = next(c for c in candidates if c.expiration == legacy_expiration)

    scored = result.selected
    legacy_evaluated = legacy_candidate is not None
    agree = (
        legacy_candidate.expiration == scored.expiration
        if legacy_candidate is not None and scored is not None
        else None
    )

    return ExpirationMethodologyComparison(
        legacy_expiration=legacy_candidate.expiration if legacy_candidate else None,
        legacy_dte=legacy_candidate.dte if legacy_candidate else None,
        scored_expiration=scored.expiration if scored else None,
        scored_dte=scored.dte if scored else None,
        scored_total_score=scored.score.total if scored else None,
        scored_liquidity=scored.score.liquidity if scored else None,
        scored_quote_coverage=scored.score.quote_coverage if scored else None,
        scored_bid_ask_quality=scored.score.bid_ask_quality if scored else None,
        scored_dte_suitability=scored.score.dte_suitability if scored else None,
        scored_data_quality=scored.score.data_quality if scored else None,
        legacy_candidate_evaluated_by_scored_engine=legacy_evaluated,
        methodologies_agree=agree,
    )
