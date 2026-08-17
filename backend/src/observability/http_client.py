"""One structured log line per outbound HTTP call (SEC EDGAR, Tiingo, Alpha
Vantage, and every LLM provider all go through httpx.Client), via httpx
request/response event hooks rather than duplicating timing code at each of
the call sites that build their own client.

Deliberately not using ``response.elapsed`` (httpx only populates it once
the response body has been fully read or closed, which doesn't line up
with when the "response" event hook actually fires for every client/mock
configuration -- pytest-httpx's mock transport hits this in practice).
Timing the request hook against the response hook via ``request.extensions``
is unaffected by that.
"""

import logging
import time

import httpx

log = logging.getLogger("http.client")

_START_TIME_KEY = "start_time"


def _record_start_time(request: httpx.Request) -> None:
    request.extensions[_START_TIME_KEY] = time.monotonic()


def _log_response(response: httpx.Response) -> None:
    start = response.request.extensions.get(_START_TIME_KEY)
    duration_ms = round((time.monotonic() - start) * 1000, 2) if start is not None else None
    log.info(
        "outbound http call",
        extra={
            "host": response.url.host,
            "method": response.request.method,
            "path": response.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )


def new_http_client(**kwargs) -> httpx.Client:
    """Drop-in replacement for ``httpx.Client(...)`` that also logs. Any
    caller-supplied ``event_hooks`` are preserved, not overwritten.
    """
    hooks = kwargs.pop("event_hooks", {})
    hooks["request"] = [*hooks.get("request", []), _record_start_time]
    hooks["response"] = [*hooks.get("response", []), _log_response]
    return httpx.Client(event_hooks=hooks, **kwargs)
