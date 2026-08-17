"""Deterministic data-freshness policy for on-demand research preparation
(Phase 14). For each class of data this project ingests, how old is "stale
enough to refresh" -- default thresholds chosen from how often that data
actually changes in reality (documented per class below), not an assumed
or unverified provider rate limit. Policies are a plain injectable dict, not
hardcoded into the assessment function, so they're genuinely configurable
rather than configurable-in-name-only.

Immutable historical data (past price bars, already-reported actuals,
already-ingested filings) is never "refreshed" once ingested -- only
checked periodically for newly-available records (a new quarter's actual,
a new filing). That's expressed here as a long-but-finite max age (worth
occasionally re-checking for something new), never a magic "never stale"
special case, since even "immutable" history needs a first check and a new
quarter eventually arrives.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class DataClass(StrEnum):
    COMPANY_METADATA = "company_metadata"
    HISTORICAL_EARNINGS = "historical_earnings"
    PRICE_HISTORY = "price_history"
    SEC_FILINGS = "sec_filings"
    FILING_EMBEDDINGS = "filing_embeddings"
    EARNINGS_ESTIMATES = "earnings_estimates"
    OPTIONS_CHAIN = "options_chain"
    PORTFOLIO_POSITIONS = "portfolio_positions"


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"


@dataclass(frozen=True)
class FreshnessPolicy:
    max_age: timedelta
    rationale: str


DEFAULT_POLICIES: dict[DataClass, FreshnessPolicy] = {
    DataClass.COMPANY_METADATA: FreshnessPolicy(
        timedelta(days=365),
        "Ticker/name/CIK/sector essentially never change; a yearly re-check catches the rare "
        "rename or re-listing.",
    ),
    DataClass.HISTORICAL_EARNINGS: FreshnessPolicy(
        timedelta(days=1),
        "A specific quarter's reported actual never changes once filed, but a NEW quarter "
        "appears roughly every 90 days -- checked daily so a just-reported quarter shows up "
        "quickly rather than waiting for the next full research request.",
    ),
    DataClass.PRICE_HISTORY: FreshnessPolicy(
        timedelta(hours=20),
        "Daily bars for past trading days are immutable; only the most recent one can still be "
        '"today\'s still-forming bar" -- a sub-daily threshold keeps that current without '
        "re-fetching years of unchanged history on every request.",
    ),
    DataClass.SEC_FILINGS: FreshnessPolicy(
        timedelta(days=1),
        "Individual filings never change once filed; a new one can appear at any time -- "
        "checked daily, matching the historical-earnings cadence they're related to.",
    ),
    DataClass.FILING_EMBEDDINGS: FreshnessPolicy(
        timedelta(days=1),
        "Only needs rebuilding when a new filing appears -- same cadence as SEC_FILINGS.",
    ),
    DataClass.EARNINGS_ESTIMATES: FreshnessPolicy(
        timedelta(hours=12),
        "Analyst consensus genuinely moves day to day; Alpha Vantage's free tier is real-time "
        "but rate-limited (see docs/data_sources.md), so this isn't refreshed on every page "
        "view either.",
    ),
    DataClass.OPTIONS_CHAIN: FreshnessPolicy(
        timedelta(hours=4),
        "The most time-sensitive data class -- refreshed more readily, but still not on every "
        "page load; a real IBKR round-trip for a bounded chain is dozens of HTTP calls (see "
        "docs/ibkr_integration.md), not something to repeat needlessly.",
    ),
    DataClass.PORTFOLIO_POSITIONS: FreshnessPolicy(
        timedelta(hours=1),
        "Real holdings change only when the user actually trades (this project places no "
        "orders) -- refreshed on request, not continuously polled.",
    ),
}


def assess_freshness(
    data_class: DataClass,
    last_updated: datetime | None,
    now: datetime,
    policies: dict[DataClass, FreshnessPolicy] = DEFAULT_POLICIES,
) -> FreshnessStatus:
    """``last_updated=None`` means the data has never been collected at
    all -- always MISSING, never STALE (a meaningfully different state: a
    preparation pipeline should *always* attempt a first fetch, regardless
    of any freshness threshold).
    """
    if last_updated is None:
        return FreshnessStatus.MISSING
    policy = policies[data_class]
    age = now - last_updated
    return FreshnessStatus.FRESH if age <= policy.max_age else FreshnessStatus.STALE


def needs_refresh(status: FreshnessStatus) -> bool:
    """MISSING and STALE both mean "go fetch it"; FRESH means "reuse what's
    already stored". A single, obvious predicate so callers never have to
    remember which enum values count as "needs work" themselves.
    """
    return status in (FreshnessStatus.MISSING, FreshnessStatus.STALE)
