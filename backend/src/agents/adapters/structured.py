"""Bounded, observable structured-output generation (requirement 12).

Three rules this module exists to enforce:

1. A schema-validation failure is *observable*. It comes back as a real
   ``StructuredOutcome`` carrying the attempt count and the error, not as
   a swallowed exception.
2. Retry is *bounded*. ``MAX_STRUCTURED_ATTEMPTS`` tries, then stop. The
   whole point of the surrounding graph is that no loop is unbounded.
3. Failure never becomes a *default value*. There is no
   ``direction="neutral"`` fallback here and there must never be one: a
   neutral view is a real judgement this project's prompts ask for
   explicitly when evidence is mixed, so manufacturing one from a parse
   error would put a fabricated judgement into the same field a real one
   occupies. Callers get ``value=None`` and decide what to say.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from agents.adapters.model import EDLChatModel
from services.llm.errors import LLMError, StructuredOutputError

#: One retry, not five. A model that cannot satisfy a schema at
#: temperature 0.0 twice in a row is not going to satisfy it on the sixth
#: attempt, and each attempt is a real billed call.
MAX_STRUCTURED_ATTEMPTS = 2


@dataclass(frozen=True)
class StructuredOutcome[SchemaT: BaseModel]:
    value: SchemaT | None
    attempts: int
    #: Set only when ``value`` is None. Already redaction-safe: these are
    #: this project's own error messages, not raw provider payloads.
    error: str | None = None
    #: Distinguishes "the model produced unusable JSON" from "the provider
    #: was unreachable" -- requirement 39 refuses to collapse those.
    error_category: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


def generate_structured_bounded[SchemaT: BaseModel](
    model: EDLChatModel,
    messages: list[BaseMessage],
    schema: type[SchemaT],
    *,
    max_attempts: int = MAX_STRUCTURED_ATTEMPTS,
    temperature: float = 0.0,
    max_tokens: int = 1024,
) -> StructuredOutcome[SchemaT]:
    """Runs ``model.with_structured_output(schema)`` up to ``max_attempts``.

    Only ``StructuredOutputError`` is retried. A ``MissingAPIKeyError`` or
    a request failure is not a parse problem and retrying it just spends
    quota on the same answer, so it returns immediately with its own
    category.
    """
    runnable = model.with_structured_output(
        schema, temperature=temperature, max_tokens=max_tokens
    )
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            value: Any = runnable.invoke(messages)
            return StructuredOutcome(value=value, attempts=attempt)
        except StructuredOutputError as exc:
            last_error = str(exc)
        except LLMError as exc:
            return StructuredOutcome(
                value=None,
                attempts=attempt,
                error=str(exc),
                error_category=type(exc).__name__,
            )
    return StructuredOutcome(
        value=None,
        attempts=max_attempts,
        error=last_error,
        error_category=StructuredOutputError.__name__,
    )
