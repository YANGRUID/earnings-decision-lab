"""Resolves the real company/companies an AI Research question is about
(Part A3/A5/A6, 2026-08-26) -- the piece that actually feeds a resolved
ticker into ``agents.orchestrator.AgentOrchestrator.run``'s
``resolved_tickers`` argument, which is what makes the deterministic
tool-argument defaulting and the system-prompt hint in
``prompts.agent_planning`` real rather than theoretical.

Two independent signals are combined:

- An explicit ticker (e.g. the Research page's own URL ``?ticker=`` query
  param, already threaded through as ``ResearchQueryRequest.ticker``) --
  always trusted first when present.
- Tickers mentioned in the free-text question itself, via
  ``extract_ticker_candidates`` below -- a real but deliberately heuristic
  regex-and-stopword scan, not an NLP entity extractor. This project has
  no ML-based ticker recognizer; a plain regex with a stopword list is
  honest about being approximate, and is bounded (see
  ``_MAX_NEW_TICKER_LOOKUPS``) so a question with several false-positive
  all-caps words never triggers a burst of real SEC network lookups.

Deliberately does not distinguish "definitely not a ticker" from
"a real ticker with no SEC record" in its output -- both come back as
unresolved. The caller (api/routers/research.py) decides what to do with
an unresolved candidate (most often: nothing, since it was probably never
a real ticker to begin with).
"""

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from models.company import Company
from providers.sec_edgar import SECEdgarProvider
from services.symbol_resolution import SymbolResolution, normalize_ticker, resolve_symbol

# Reuses resolve_symbol's own ticker shape, applied here to free text
# instead of a single trusted field -- same 1-6 letters, optional
# dot/hyphen share-class suffix.
_TICKER_TOKEN = re.compile(r"\b[A-Z]{1,6}(?:[.\-][A-Z]{1,2})?\b")

# Common short/medium all-caps English words and finance/tech jargon that
# would otherwise look like a plausible ticker to the regex above --
# not exhaustive by design (see this module's own docstring): a false
# positive that survives this list still has to fail a real SEC lookup
# (or fail to match an existing Company row) before it's ever treated as
# a resolved company, so the cost of an incomplete list is one wasted
# lookup per question, not a wrong answer.
_STOPWORDS = frozenset(
    {
        "A",
        "I",
        "AN",
        "THE",
        "IS",
        "ARE",
        "WAS",
        "WERE",
        "BE",
        "BEEN",
        "BEING",
        "DO",
        "DOES",
        "DID",
        "HAS",
        "HAVE",
        "HAD",
        "CAN",
        "COULD",
        "WILL",
        "WOULD",
        "SHALL",
        "SHOULD",
        "MAY",
        "MIGHT",
        "MUST",
        "NOT",
        "NO",
        "YES",
        "OK",
        "AND",
        "OR",
        "BUT",
        "IF",
        "SO",
        "AS",
        "AT",
        "BY",
        "FOR",
        "FROM",
        "IN",
        "INTO",
        "OF",
        "ON",
        "TO",
        "UP",
        "WITH",
        "VS",
        "VS.",
        "WHAT",
        "WHEN",
        "WHERE",
        "WHY",
        "HOW",
        "WHO",
        "WHICH",
        "THIS",
        "THAT",
        "THESE",
        "THOSE",
        "IT",
        "ITS",
        "THEY",
        "THEM",
        "THEIR",
        "WE",
        "US",
        "OUR",
        "YOU",
        "YOUR",
        "ME",
        "MY",
        "HE",
        "SHE",
        "HIS",
        "HER",
        "ANALYZE",
        "ANALYSE",
        "COMPARE",
        "EXPLAIN",
        "SUMMARIZE",
        "SUMMARISE",
        "TELL",
        "SHOW",
        "GIVE",
        "FIND",
        "GET",
        "LIST",
        "DESCRIBE",
        "LATEST",
        "RECENT",
        "LAST",
        "NEXT",
        "NEW",
        "OLD",
        "CURRENT",
        "EARNINGS",
        "REPORT",
        "REPORTS",
        "RESULTS",
        "RESULT",
        "GUIDANCE",
        "REVENUE",
        "PROFIT",
        "LOSS",
        "INCOME",
        "MARGIN",
        "MARGINS",
        "STOCK",
        "STOCKS",
        "SHARE",
        "SHARES",
        "PRICE",
        "PRICES",
        "MARKET",
        "OPTION",
        "OPTIONS",
        "CALL",
        "CALLS",
        "PUT",
        "PUTS",
        "STRIKE",
        "STRIKES",
        "QUARTER",
        "QUARTERLY",
        "ANNUAL",
        "YEAR",
        "YEARS",
        "MONTH",
        "MONTHS",
        "DAY",
        "DAYS",
        "WEEK",
        "WEEKS",
        "TODAY",
        "YESTERDAY",
        "TOMORROW",
        "COMPANY",
        "COMPANIES",
        "BUSINESS",
        "INDUSTRY",
        "SECTOR",
        "CEO",
        "CFO",
        "COO",
        "CTO",
        "SEC",
        "EPS",
        "GAAP",
        "IPO",
        "ETF",
        "FY",
        "YOY",
        "QOQ",
        "TTM",
        "YTD",
        "EOD",
        "ATM",
        "ITM",
        "OTM",
        "ROI",
        "IV",
        "AI",
        "ML",
        "API",
        "URL",
        "FAQ",
        "ASAP",
        "EDGAR",
        "CIK",
        "USA",
        "UK",
        "EU",
        "NYSE",
        "NASDAQ",
        "DTE",
        "RAG",
        "LLM",
        "PLEASE",
        "THANKS",
        "THANK",
        "HI",
        "HELLO",
        "HEY",
    }
)

# Bounds how many candidate tokens that DON'T already match an existing
# Company row get a real SEC network lookup for one question -- keeps a
# question with several false-positive all-caps words (or several
# genuinely new companies at once) from turning into an unbounded burst
# of real network calls. A question naming more distinct new companies
# than this in one go is a genuinely rare shape; the ones beyond the
# bound are simply not resolved this pass, not silently dropped from
# consideration forever (a follow-up question resolves them normally).
_MAX_NEW_TICKER_LOOKUPS = 4


def extract_ticker_candidates(question: str) -> list[str]:
    """Every all-caps, ticker-shaped token in ``question`` that isn't a
    known non-ticker word, in first-appearance order, deduplicated.
    Heuristic by design -- see this module's own docstring."""
    seen: list[str] = []
    for match in _TICKER_TOKEN.finditer(question):
        token = match.group(0)
        if token in _STOPWORDS or token in seen:
            continue
        seen.append(token)
    return seen


@dataclass(frozen=True)
class ResolvedQueryCompanies:
    resolved: list[SymbolResolution]
    unresolved: list[str]

    @property
    def tickers(self) -> list[str]:
        return [r.ticker for r in self.resolved]


def resolve_mentioned_companies(
    db: Session,
    edgar: SECEdgarProvider,
    question: str,
    explicit_ticker: str | None = None,
) -> ResolvedQueryCompanies:
    """Combines an explicit ticker (trusted, always tried first) with
    tickers mentioned in the question text. A candidate that already has
    a ``Company`` row resolves with no network call at all; a genuinely
    new candidate gets one real SEC lookup, bounded by
    ``_MAX_NEW_TICKER_LOOKUPS``."""
    candidates = extract_ticker_candidates(question)
    if explicit_ticker:
        normalized = normalize_ticker(explicit_ticker)
        candidates = [normalized] + [c for c in candidates if c != normalized]

    if not candidates:
        return ResolvedQueryCompanies(resolved=[], unresolved=[])

    existing_rows = {
        company.ticker: company
        for company in db.query(Company).filter(Company.ticker.in_(candidates)).all()
    }

    resolved: list[SymbolResolution] = []
    unresolved: list[str] = []
    new_lookups = 0
    for candidate in candidates:
        existing = existing_rows.get(candidate)
        if existing is not None:
            resolved.append(
                SymbolResolution(
                    ticker=candidate,
                    supported=True,
                    reason=None,
                    cik=existing.cik,
                    existing_company=existing,
                )
            )
            continue
        if new_lookups >= _MAX_NEW_TICKER_LOOKUPS:
            continue
        new_lookups += 1
        resolution = resolve_symbol(db, edgar, candidate)
        if resolution.supported:
            resolved.append(resolution)
        else:
            unresolved.append(candidate)

    return ResolvedQueryCompanies(resolved=resolved, unresolved=unresolved)
