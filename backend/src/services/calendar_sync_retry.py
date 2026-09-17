"""Whether a failed calendar sync deserves another attempt today, and when.

Why this exists (2026-09-17 hardening). The nightly calendar sync was a single
attempt. Each individual HTTP request is already retried inside the provider
adapters (tenacity, three attempts, transport errors / 5xx / a non-quota 429 --
see providers/earningsapi.py::_retryable), but once every provider in the chain
had failed, the RUN was over and nothing tried again until the next night's
cron. Measured on this deployment: the 2026-09-15 20:58 ET run died on
``[Errno -2] Name or service not known`` -- the Mac had just woken and DNS was
not up yet -- and the calendar then went 23 hours without a refresh, through a
15:30 ET decision window, because of a resolver that was working again seconds
later.

Two rules keep the retry honest rather than merely persistent:

* **Bounded.** Two extra attempts, at widening delays, and then the failure
  stands. A provider that is genuinely down must surface as a failure in
  Operations, not be hidden behind an unbounded loop.
* **Only for failures that could plausibly succeed on a retry.** A spent plan
  allowance is the case that matters: EarningsAPI answers a refusal fast and
  counts it, so retrying one is worse than useless -- it spends tomorrow's
  allowance to learn a fact this run already knows (measured 2026-09-10 ..
  09-17: a nightly sync took up to 455 seconds re-learning it ~100 times).
  Invalid credentials and a malformed provider contract are equally settled:
  nothing about waiting five minutes changes them.

Each attempt is its own scheduler_run row, so "failed, then succeeded on
retry" is visible as exactly that -- never one opaque success (or one opaque
failure) covering several tries.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

#: Attempts a single scheduled sync may make in total, the first included.
MAX_SYNC_ATTEMPTS = 3

#: Delay before attempt N+1. Short enough that the calendar is refreshed long
#: before any decision window, long enough for a waking machine's network, a
#: provider blip or a per-minute rate limit to clear.
_RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=20))


def retry_delay_for(attempt: int) -> timedelta | None:
    """How long to wait before attempt ``attempt + 1``, or None when the
    attempts are spent (``attempt`` is 1-based)."""
    index = attempt - 1
    if index < 0 or index >= len(_RETRY_DELAYS):
        return None
    return _RETRY_DELAYS[index]


def _one_failure_is_transient(exc: BaseException) -> bool:
    """Whether this single provider failure could plausibly succeed later
    today, read from the real signals the failure actually carries -- never
    from its message text.

    A spent allowance is reported by the adapters as ``quota_exhausted`` (see
    providers/earningsapi.py::QUOTA_EXHAUSTED_CODES) and is checked first,
    because it arrives as a 429 and would otherwise read as an ordinary rate
    limit.
    """
    if getattr(exc, "quota_exhausted", None):
        return False
    if getattr(exc, "rate_limited", False):
        # A per-minute limit, which clears on its own -- unlike the plan
        # allowance above, which does not clear until the period rolls over.
        return True
    # The adapters raise their own error types for an HTTP response but let a
    # transport failure through untouched, and they chain the original with
    # ``raise ... from exc``. Walking the chain reads the real cause of a
    # wrapped failure instead of re-deriving it from a formatted string.
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.TransportError):
            # DNS, connection reset, timeout, TLS handshake.
            return True
        if isinstance(current, httpx.HTTPStatusError):
            status = current.response.status_code
            # 5xx is the provider's own fault and typically brief. Every other
            # status here is settled: 401/403 (credentials), 404 (contract),
            # and a 429 that reached this point without a quota code was
            # already covered above.
            return status >= 500
        current = current.__cause__ or current.__context__
    return False


def is_transient_sync_failure(exc: BaseException) -> bool:
    """Whether a failed sync should be attempted again today.

    For a whole-chain failure (providers/fallback.py::AllProvidersFailedError)
    the answer is yes when ANY provider's failure was transient: the chain only
    needs one provider to answer, so a spent primary alongside a fallback that
    hit a DNS error is still worth retrying -- the chain will skip the spent
    primary on its own (``EarningsCalendarProviderChain.exhausted``).
    """
    errors = getattr(exc, "errors", None)
    if isinstance(errors, list) and errors:
        return any(_one_failure_is_transient(inner) for _, inner in errors)
    return _one_failure_is_transient(exc)
