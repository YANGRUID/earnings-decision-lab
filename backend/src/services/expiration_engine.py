"""Options Decision Engine V3 Part C: orchestrates real expiration
discovery + evaluation on top of the pure scoring in
analytics/options/expiration_selection.py. This is the only place that
calls a provider more than once to compare candidate expirations against
each other -- every caller (Strategy Lab, AI Decision) goes through here
instead of duplicating live-fetch logic.

Auto mode discovers real candidate expirations via
``provider.list_available_expirations`` and fetches each one's real chain
to score it -- never invents a date or a contract. Manual mode fetches
exactly the one expiration the user chose and, when requested, compares it
against what Auto would have picked so a materially-worse manual choice
can be flagged (never blocked, unless genuinely invalid).
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.options.expiration_selection import (
    ExpirationCandidate,
    ExpirationSelectionResult,
    build_expiration_candidate,
    build_expiration_reasons,
    select_best_expiration,
)
from models.company import Company
from providers.base import OptionsDataProvider
from providers.ibkr_client import IBKRError
from services.options_analytics import _latest_close_price_on_or_before

# "At minimum consider: nearest valid post-earnings weekly, next weekly,
# next monthly / longer-dated alternative when available" (Part 9) -- three
# real, live-fetched candidates by default. Each candidate costs one real
# provider round-trip (IBKR: ~4s incl. its documented snapshot-priming
# delay), so this is deliberately not larger without a caller asking for it.
DEFAULT_MAX_CANDIDATES = 3

# A manual expiration is flagged (never blocked) when its total score
# trails Auto's pick by at least this many points -- "materially worse",
# not merely "not the top pick by one point".
_MATERIALLY_WORSE_SCORE_GAP = 20


def _underlying_price_for(db: Session, company: Company, as_of: datetime) -> Decimal | None:
    return _latest_close_price_on_or_before(db, company.ticker, as_of.date())


def _evaluate_expiration(
    provider: OptionsDataProvider,
    company: Company,
    as_of: datetime,
    reference_date: date,
    earnings_date: date | None,
    expiration: date,
    underlying_price: Decimal | None,
) -> ExpirationCandidate:
    quotes = provider.get_option_chain(company.ticker, as_of, expiration=expiration)
    return build_expiration_candidate(
        expiration=expiration,
        reference_date=reference_date,
        earnings_date=earnings_date,
        quotes=quotes,
        underlying_price=underlying_price,
    )


def resolve_auto_expiration(
    db: Session,
    company: Company,
    provider: OptionsDataProvider,
    as_of: datetime,
    earnings_date: date | None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> ExpirationSelectionResult:
    """Discovers up to ``max_candidates`` real listed expirations after the
    earnings date (or after ``as_of`` in general mode), fetches each one's
    real chain, and picks the highest-scoring one that actually covers the
    event and has at least one priceable contract. Returns a result with
    ``selected=None`` (never a fabricated pick) when the provider has no
    real expirations to offer or none are usable.
    """
    reference_date = earnings_date if earnings_date is not None else as_of.date()
    try:
        discovered = provider.list_available_expirations(
            company.ticker, after=reference_date, max_candidates=max_candidates
        )
    except IBKRError as exc:
        return ExpirationSelectionResult(
            mode="auto",
            selected=None,
            warning=f"The options provider is unavailable right now ({exc}).",
        )
    if not discovered:
        return ExpirationSelectionResult(
            mode="auto",
            selected=None,
            warning="No real listed expirations were found after the reference date.",
        )

    underlying_price = _underlying_price_for(db, company, as_of)
    try:
        candidates = [
            _evaluate_expiration(
                provider,
                company,
                as_of,
                reference_date,
                earnings_date,
                expiration,
                underlying_price,
            )
            for expiration in discovered
        ]
    except IBKRError as exc:
        return ExpirationSelectionResult(
            mode="auto",
            selected=None,
            warning=f"The options provider is unavailable right now ({exc}).",
        )

    selected = select_best_expiration(candidates)
    if selected is None:
        return ExpirationSelectionResult(
            mode="auto",
            selected=None,
            alternatives=candidates,
            warning="None of the discovered expirations had a usable "
            "(priceable, post-earnings) chain.",
        )

    alternatives = [c for c in candidates if c.expiration != selected.expiration]
    reasons = build_expiration_reasons(selected, alternatives)
    return ExpirationSelectionResult(
        mode="auto", selected=selected, alternatives=alternatives, reasons=reasons
    )


def resolve_manual_expiration(
    db: Session,
    company: Company,
    provider: OptionsDataProvider,
    as_of: datetime,
    earnings_date: date | None,
    chosen_expiration: date,
    *,
    compare_against_auto: bool = True,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> ExpirationSelectionResult:
    """Fetches exactly the user-chosen expiration's real chain -- every
    contract, Greek, strike, and downstream strategy must come only from
    this expiration (Part 15), never silently substituted. When
    ``compare_against_auto`` is set, also runs Auto to attach a warning if
    the manual choice scores materially worse; this never blocks the
    manual choice, only labels it (Part 16)."""
    reference_date = earnings_date if earnings_date is not None else as_of.date()
    underlying_price = _underlying_price_for(db, company, as_of)
    try:
        selected = _evaluate_expiration(
            provider,
            company,
            as_of,
            reference_date,
            earnings_date,
            chosen_expiration,
            underlying_price,
        )
    except IBKRError as exc:
        return ExpirationSelectionResult(
            mode="manual",
            selected=None,
            warning=f"The options provider is unavailable right now ({exc}).",
        )

    warning: str | None = None
    alternatives: list[ExpirationCandidate] = []
    if selected.contract_count == 0:
        warning = (
            "This expiration has no real listed contracts near the current underlying price."
        )
    elif selected.excluded_pre_earnings:
        warning = (
            "This expiration is on or before the earnings date -- it will not cover the event."
        )

    if compare_against_auto and warning is None:
        auto_result = resolve_auto_expiration(
            db, company, provider, as_of, earnings_date, max_candidates=max_candidates
        )
        if auto_result.selected is not None:
            alternatives = [auto_result.selected, *auto_result.alternatives]
            gap = auto_result.selected.score.total - selected.score.total
            if gap >= _MATERIALLY_WORSE_SCORE_GAP:
                warning = (
                    f"This expiration has materially worse execution quality than the Auto "
                    f"recommendation ({auto_result.selected.expiration.isoformat()}, "
                    f"{selected.score.total}/100 vs. {auto_result.selected.score.total}/100). "
                    f"Liquidity: {selected.quality}. "
                    f"Quote coverage: {int(selected.quote_coverage * 100)}%. "
                    f"DTE: {selected.dte}."
                )

    return ExpirationSelectionResult(
        mode="manual", selected=selected, alternatives=alternatives, warning=warning
    )
