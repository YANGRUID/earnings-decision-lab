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
from datetime import UTC, datetime
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
) -> None:
    if db is None:
        return
    try:
        db.add(
            ProviderUsageEvent(
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
                provider_units=None,
                estimated_cost=None,
            )
        )
        db.commit()
    except Exception:
        log.warning("failed to record provider usage event", exc_info=True)
        db.rollback()


class _InstrumentedDataProvider:
    """A generic, attribute-forwarding proxy -- works uniformly across
    MarketDataProvider/OptionsDataProvider/EarningsEstimatesProvider/
    SECEdgarProvider without a per-domain wrapper, since every real call a
    data adapter makes is exactly one public method call. Every callable
    attribute is timed and recorded as one ProviderUsageEvent; non-callable
    attributes pass through untouched."""

    def __init__(self, inner: Any, db: Session | None, provider: str, domain: str) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_domain", domain)

    def __getattr__(self, item: str) -> Any:
        attr = getattr(self._inner, item)
        if not callable(attr) or item.startswith("_"):
            return attr

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
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
                )
                raise
            record_usage_event(
                self._db,
                provider=self._provider,
                domain=self._domain,
                operation=item,
                success=True,
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            return result

        return wrapped


def instrument_data_provider[T](
    inner: T, db: Session | None, provider: str, domain: str
) -> T:
    """Wraps any data-provider adapter instance for usage tracking. Typed
    as returning ``T`` (the caller's own provider Protocol) since the proxy
    forwards every method with the same signature -- callers keep type-
    checking against MarketDataProvider/OptionsDataProvider/etc. unchanged."""
    return _InstrumentedDataProvider(inner, db, provider, domain)  # type: ignore[return-value]


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
