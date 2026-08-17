"""Structured (JSON) logging setup. Every log line is a JSON object with at
least ``timestamp``, ``level``, ``message`` — machine-parseable from day
one, not upgraded later. Request-scoped fields (request_id, duration_ms,
status_code) are attached via ``extra=`` at the call site (see
api/middleware.py), not by reformatting strings.
"""

import json
import logging
import sys
from datetime import UTC, datetime

_RESERVED_LOG_RECORD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # httpx (and httpcore underneath it) log their own "HTTP Request: GET
    # <full-url> ..." line at INFO, and several of this project's providers
    # (Tiingo, Alpha Vantage) authenticate via an API key in the URL query
    # string — at the default INFO level that line would print a real
    # secret into structured logs. observability/http_client.py's own
    # per-call log line (host + path only, no query string) is the safe,
    # intentional replacement, so httpx/httpcore's own logging is muted
    # rather than merely "not relied upon."
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
