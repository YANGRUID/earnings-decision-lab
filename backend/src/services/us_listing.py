"""US-listing check for business eligibility (v4.0.1).

"US listed" means the security trades on a US exchange -- NOT that the
company is domiciled in the United States. The earnings calendar's
``country`` field is the domicile (Lululemon: CA, Medtronic: IE), so a
country-only rule wrongly rejected US-listed companies. SEC's own
``company_tickers_exchange.json`` names the listing exchange of every
SEC-registered ticker; that is the honest source. The map is fetched once
and cached in-process for a day; callers inject the lookup so tests never
reach the network, and a lookup failure is reported as *unverified*, never
as "not listed".
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from core.config import get_settings
from providers.sec_edgar import SECEdgarProvider

log = logging.getLogger("services.us_listing")

UsListingLookup = Callable[[str], str | None]

_CACHE_TTL_SECONDS = 24 * 3600


def sec_ticker_form(symbol: str) -> str:
    """SEC writes class shares with a dash (``BRK-B``); calendars use a dot."""
    return symbol.strip().upper().replace(".", "-")


class UsListingCheck:
    """``exchange_for(symbol)`` -> the US exchange SEC lists the ticker on,
    or ``None`` when SEC does not list it. Raises when the list cannot be
    fetched (the caller decides how to report an unverified check)."""

    def __init__(self, edgar: SECEdgarProvider, *, ttl_seconds: float = _CACHE_TTL_SECONDS):
        self._edgar = edgar
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._listings: dict[str, str] | None = None
        self._loaded_at = 0.0

    def _load(self) -> dict[str, str]:
        with self._lock:
            fresh = self._listings is not None and time.monotonic() - self._loaded_at < self._ttl
            if not fresh:
                self._listings = self._edgar.list_exchange_listings()
                self._loaded_at = time.monotonic()
            assert self._listings is not None
            return self._listings

    def exchange_for(self, symbol: str) -> str | None:
        listings = self._load()
        return listings.get(sec_ticker_form(symbol)) or listings.get(symbol.strip().upper())


_default: UsListingCheck | None = None
_default_lock = threading.Lock()


def default_us_listing() -> UsListingLookup:
    """The process-wide cached check, built from the configured SEC user agent."""
    global _default
    with _default_lock:
        if _default is None:
            settings = get_settings()
            _default = UsListingCheck(SECEdgarProvider(user_agent=settings.sec_edgar_user_agent))
        return _default.exchange_for
