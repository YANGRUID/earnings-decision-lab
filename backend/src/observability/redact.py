"""Strips credential-shaped values out of arbitrary text before it's logged
or returned to a client. Exists because two real leak paths were found live
during Phase 10 observability work (see docs/engineering_decisions.md):

- httpx bakes the full request URL (query string included) into
  ``str(httpx.HTTPStatusError)``, and this project's Tiingo/Alpha Vantage
  adapters authenticate via a ``token``/``apikey`` query parameter.
- A DB driver's connection-failure message can echo the DSN it was given,
  and a Postgres DSN carries its password in the URL's userinfo component
  (``postgresql://user:PASSWORD@host/db``), not a query parameter.

Both are real, not theoretical: neither was invented for this module.
"""

import re

_SENSITIVE_QUERY_PARAMS = ("token", "apikey", "api_key", "key", "access_token")
_QUERY_PARAM_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SENSITIVE_QUERY_PARAMS) + r")=[^&\s]+",
)
_URL_USERINFO_PATTERN = re.compile(r"(?i)(://[^:/\s@]+:)[^@/\s]+(@)")


def redact(text: str) -> str:
    text = _QUERY_PARAM_PATTERN.sub(lambda m: f"{m.group(1)}=REDACTED", text)
    text = _URL_USERINFO_PATTERN.sub(lambda m: f"{m.group(1)}REDACTED{m.group(2)}", text)
    return text
