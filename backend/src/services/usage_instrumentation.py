"""Central provider-call instrumentation for the API Usage dashboard (see
Phase 14.10 Part C/L). Every real call made through a provider adapter --
data or LLM -- flows through exactly two seams: providers/factory.py
(data providers) and services/llm/factory.py + api/deps.py::get_llm (LLM
providers). Wrapping the constructed provider object at those two seams,
rather than adding a usage-recording call inside every endpoint, is what
keeps this "central" per the cycle's explicit instruction -- individual
routers and services never import this module directly.

Never records: API keys, authorization headers, IBKR session cookies, or
request/response bodies -- only the shape of the call (provider, domain,
operation, timing, outcome), matching ProviderUsageEvent's own columns.
A recording failure (e.g. a DB hiccup) is swallowed, logged, and never
allowed to break the real call it's observing.
"""

import logging
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.provider_usage_event import ProviderUsageEvent
from providers.alpha_vantage import AlphaVantageError
from providers.ibkr_client import IBKRRateLimitedError
from services.llm.base import LLMProvider
from services.llm.types import ChatMessage, GenerateResult, ToolDefinition

SchemaT = TypeVar("SchemaT", bound=BaseModel)

log = logging.getLogger("services.usage_instrumentation")


def _classify_exception(exc: Exception) -> tuple[str | None, bool]:
    """(status_code, rate_limited) from whatever real signal the exception
    actually carries -- never guessed beyond what the provider itself
    reported."""
    if isinstance(exc, AlphaVantageError):
        return ("rate_limited" if exc.rate_limited else "error"), exc.rate_limited
    if isinstance(exc, IBKRRateLimitedError):
        return "429", True
    # Calendar adapters (EarningsAPI, Finnhub) carry their verdict on the
    # exception itself. Before this was read, every one of EarningsAPI's
    # refusals from 2026-09-10 onward was recorded as rate_limited=False with
    # no status, so the spent quota was invisible in the usage data.
    quota_code = getattr(exc, "quota_exhausted", None)
    if quota_code:
        return str(quota_code)[:32], True
    rate_limited = getattr(exc, "rate_limited", None)
    if isinstance(rate_limited, bool):
        return ("429" if rate_limited else "error"), rate_limited
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return str(code), code == 429
    return None, False


def record_usage_event(
    db: Session | None,
    *,
    provider: str,
    domain: str,
    operation: str,
    success: bool,
    latency_ms: int,
    status_code: str | None = None,
    rate_limited: bool = False,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    reasoning_tokens: int | None = None,
    cache_hit_tokens: int | None = None,
    provider_units: Decimal | int | None = None,
    credential_fingerprint: str | None = None,
) -> None:
    if db is None:
        return
    event = ProviderUsageEvent(
        provider=provider,
        domain=domain,
        operation=operation,
        occurred_at=datetime.now(UTC),
        success=success,
        latency_ms=latency_ms,
        status_code=status_code,
        rate_limited=rate_limited,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        model=model,
        reasoning_effort=reasoning_effort,
        reasoning_tokens=reasoning_tokens,
        cache_hit_tokens=cache_hit_tokens,
        provider_units=provider_units,
        estimated_cost=None,
        credential_fingerprint=credential_fingerprint,
    )
    # Telemetry gets its OWN transaction, never the caller's.
    #
    # Measured defect (2026-09-18 .. 09-21). This used to add the row to the
    # caller's session and commit it. A commit flushes everything pending in
    # that session, so a provider call made in the middle of building something
    # else committed that work early -- and when the flush failed, the caller's
    # rows were rolled back and the failure was logged here as "failed to
    # record provider usage event". The earnings calendar sync hit exactly
    # that: one un-storable provider estimate (see services/
    # earnings_calendar_sync.py::_storable_estimate) surfaced as a swallowed
    # usage-recording warning while the sync itself reported success, so nights
    # that lost calendar writes looked healthy.
    #
    # A short-lived session on the same connection pool keeps the two apart in
    # both directions: telemetry can no longer commit or discard business
    # state, and a business rollback no longer erases the telemetry.
    session: Session | None = None
    try:
        bind = db.get_bind()
        session = Session(bind=bind)
        session.add(event)
        session.commit()
    except Exception:
        log.warning("failed to record provider usage event", exc_info=True)
        if session is not None:
            session.rollback()
    finally:
        if session is not None:
            session.close()


def _request_units(operation: str, args: tuple[Any, ...]) -> int | None:
    """Upper bound on real HTTP requests one call makes, where the adapter's
    shape makes it knowable: EarningsAPI's calendar has no range endpoint, so a
    ``get_earnings_calendar(from, to)`` call is one request per date. Recorded
    so a free plan's daily/monthly allowance can be tracked from usage rows."""
    if operation == "get_earnings_calendar" and len(args) >= 2:
        first, last = args[0], args[1]
        if isinstance(first, date) and isinstance(last, date) and last >= first:
            return (last - first).days + 1
    return None


class _InstrumentedDataProvider:
    """A generic, attribute-forwarding proxy -- works uniformly across
    MarketDataProvider/OptionsDataProvider/EarningsEstimatesProvider/
    SECEdgarProvider without a per-domain wrapper, since every real call a
    data adapter makes is exactly one public method call. Every callable
    attribute is timed and recorded as one ProviderUsageEvent; non-callable
    attributes pass through untouched."""

    def __init__(  # noqa: PLR0913 -- one wrapper, one row's worth of identity
        self,
        inner: Any,
        db: Session | None,
        provider: str,
        domain: str,
        credential_fingerprint: str | None = None,
    ) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_domain", domain)
        object.__setattr__(self, "_credential", credential_fingerprint)

    def __getattr__(self, item: str) -> Any:
        attr = getattr(self._inner, item)
        if not callable(attr) or item.startswith("_"):
            return attr

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            units = _request_units(item, args)
            try:
                result = attr(*args, **kwargs)
            except Exception as exc:
                status_code, rate_limited = _classify_exception(exc)
                record_usage_event(
                    self._db,
                    provider=self._provider,
                    domain=self._domain,
                    operation=item,
                    success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    status_code=status_code,
                    rate_limited=rate_limited,
                    provider_units=units,
                    credential_fingerprint=self._credential,
                )
                raise
            record_usage_event(
                self._db,
                provider=self._provider,
                domain=self._domain,
                operation=item,
                success=True,
                latency_ms=int((time.monotonic() - start) * 1000),
                provider_units=units,
                credential_fingerprint=self._credential,
            )
            return result

        return wrapped


def instrument_data_provider[T](
    inner: T,
    db: Session | None,
    provider: str,
    domain: str,
    credential_fingerprint: str | None = None,
) -> T:
    """Wraps any data-provider adapter instance for usage tracking. Typed
    as returning ``T`` (the caller's own provider Protocol) since the proxy
    forwards every method with the same signature -- callers keep type-
    checking against MarketDataProvider/OptionsDataProvider/etc. unchanged.

    ``credential_fingerprint``: which key this adapter was built with (see
    services/secret_store/resolver.py::secret_fingerprint), so usage rows stay
    attributable across a key rotation. Optional -- an adapter with no key
    concept (SEC EDGAR, IBKR) passes nothing."""
    return _InstrumentedDataProvider(  # type: ignore[return-value]
        inner, db, provider, domain, credential_fingerprint
    )


class InstrumentedLLMProvider(LLMProvider):
    """Wraps a real LLMProvider so every generate/generate_structured/
    stream call is recorded once, centrally -- see api/deps.py::get_llm,
    the one place every LLM-backed endpoint constructs its provider."""

    def __init__(self, inner: LLMProvider, db: Session | None, provider: str) -> None:
        self._inner = inner
        self._db = db
        self._provider = provider
        self.name = inner.name
        self.model = inner.model
        self.capabilities = inner.capabilities

    def _record(
        self, operation: str, start: float, success: bool, usage: Any, exc: Exception | None
    ) -> None:
        status_code, rate_limited = _classify_exception(exc) if exc else (None, False)
        record_usage_event(
            self._db,
            provider=self._provider,
            domain="llm",
            operation=operation,
            success=success,
            latency_ms=int((time.monotonic() - start) * 1000),
            status_code=status_code,
            rate_limited=rate_limited,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            total_tokens=(usage.input_tokens + usage.output_tokens) if usage else None,
        )

    def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> GenerateResult:
        start = time.monotonic()
        try:
            result = self._inner.generate(
                messages, tools=tools, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as exc:
            self._record("generate", start, False, None, exc)
            raise
        self._record("generate", start, True, result.usage, None)
        return result

    def generate_structured(
        self,
        messages: list[ChatMessage],
        schema: type[SchemaT],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> SchemaT:
        start = time.monotonic()
        try:
            result = self._inner.generate_structured(
                messages, schema, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as exc:
            self._record("generate_structured", start, False, None, exc)
            raise
        # Structured calls don't return a GenerateResult (just the parsed
        # schema instance), so no provider-reported token count exists to
        # record here -- never estimated.
        self._record("generate_structured", start, True, None, None)
        return result

    def stream(
        self, messages: list[ChatMessage], *, temperature: float = 0.0, max_tokens: int = 1024
    ) -> Iterator[str]:
        start = time.monotonic()

        def _wrap() -> Iterator[str]:
            try:
                yield from self._inner.stream(
                    messages, temperature=temperature, max_tokens=max_tokens
                )
            except Exception as exc:
                self._record("stream", start, False, None, exc)
                raise
            else:
                # A stream's own chunks never carry a final usage total in
                # this project's provider interface -- recorded with no
                # token counts rather than estimated.
                self._record("stream", start, True, None, None)

        return _wrap()
