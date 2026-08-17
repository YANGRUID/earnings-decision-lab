"""Resolves a user-entered ticker into either an already-researched
``Company`` row or a real, SEC-confirmed CIK ready for
``services.research_orchestration.prepare_company_research`` to bootstrap.
This is the entry point for the on-demand research workflow (Phase 14) --
the product no longer assumes only the original four tickers exist.

Deliberately does *not* create a ``Company`` row itself, even when a new
ticker resolves successfully -- that's the preparation pipeline's job
(``services/research_orchestration.py``, which reuses
``ingestion.bootstrap_phase1``'s real per-company bootstrap logic rather
than duplicating it here). This module's only job is: is this something we
can honestly research, and do we already have it.
"""

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from models.company import Company
from providers.sec_edgar import SECEdgarProvider

# 1-6 letters, optionally a dot/hyphen share-class suffix (e.g. BRK.B,
# BF.B) -- covers the overwhelming majority of real US-listed equity
# tickers without a network call. Not exhaustive by design: anything that
# fails this still gets a real SEC lookup attempt if it's plausible input,
# not silently rejected -- see resolve_symbol.
_PLAUSIBLE_TICKER = re.compile(r"^[A-Z]{1,6}([.\-][A-Z]{1,2})?$")


def normalize_ticker(raw: str) -> str:
    return raw.strip().upper()


@dataclass(frozen=True)
class SymbolResolution:
    ticker: str
    supported: bool
    reason: str | None
    cik: str | None
    existing_company: Company | None


def resolve_symbol(db: Session, edgar: SECEdgarProvider, raw_ticker: str) -> SymbolResolution:
    ticker = normalize_ticker(raw_ticker)

    if not ticker:
        return SymbolResolution(
            ticker=ticker, supported=False, reason="empty ticker", cik=None, existing_company=None
        )
    if not _PLAUSIBLE_TICKER.match(ticker):
        return SymbolResolution(
            ticker=ticker,
            supported=False,
            reason=f"{ticker!r} doesn't look like a real US equity ticker",
            cik=None,
            existing_company=None,
        )

    existing = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if existing is not None:
        return SymbolResolution(
            ticker=ticker,
            supported=True,
            reason=None,
            cik=existing.cik,
            existing_company=existing,
        )

    cik = edgar.lookup_cik(ticker)
    if cik is None:
        return SymbolResolution(
            ticker=ticker,
            supported=False,
            reason=(
                f"{ticker!r} was not found in SEC's own company ticker list -- it may not be a "
                "US-listed equity, or the ticker may be incorrect"
            ),
            cik=None,
            existing_company=None,
        )

    return SymbolResolution(
        ticker=ticker, supported=True, reason=None, cik=cik, existing_company=None
    )
