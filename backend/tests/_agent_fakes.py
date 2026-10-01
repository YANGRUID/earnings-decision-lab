"""Shared fakes for BOTH agent runtimes.

Extracted from tests/test_agents_orchestrator.py so the legacy
orchestrator, the LangGraph workflow and the parity harness are all driven
by the *same* scripted provider and the *same* stub embedder. That is the
point: a parity comparison in which each side has its own lookalike fake
proves only that two fakes agree (requirement 44 -- "on the same frozen
tool fixtures").
"""

from collections import defaultdict
from collections.abc import Iterator

from pydantic import BaseModel

from models.document_chunk import EMBEDDING_DIM
from rag.embeddings import EmbeddingProvider
from services.llm.base import LLMProvider
from services.llm.errors import LLMRequestError
from services.llm.types import Capabilities


class _StubEmbedder(EmbeddingProvider):
    """Subclasses the real ABC on purpose.

    Nothing in the legacy path validated this -- it duck-typed the embedder
    everywhere -- but EmbeddingProvider IS an ABC, so the project's own
    intent is nominal. The retriever adapter is a pydantic model and
    enforces it, which is what surfaced the gap. A fake standing in for a
    contract should satisfy the contract.
    """

    model_name = "stub"
    dimension = EMBEDDING_DIM

    def embed(self, texts):
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


class _ScriptedLLM(LLMProvider):
    """Returns pre-scripted responses in call order, separately queued per
    schema for generate_structured and in a flat queue for generate. Raises
    LLMRequestError once a queue is exhausted (or if a queued item is
    itself an exception instance) — used to test failure recovery.
    """

    name = "scripted"

    def __init__(
        self,
        model: str = "deepseek-v4-flash",
        supports_tool_calling: bool = True,
        structured_responses: dict | None = None,
        generate_responses: list | None = None,
    ) -> None:
        self.model = model
        self.capabilities = Capabilities(
            supports_structured_output=True,
            supports_tool_calling=supports_tool_calling,
            supports_streaming=False,
        )
        self._structured_queue: dict = defaultdict(list)
        for schema, items in (structured_responses or {}).items():
            self._structured_queue[schema] = list(items)
        self._generate_queue = list(generate_responses or [])
        self.generate_calls: list = []
        self.generate_structured_calls: list = []

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=1024):
        self.generate_calls.append((messages, tools))
        if not self._generate_queue:
            raise LLMRequestError("scripted generate() queue exhausted")
        item = self._generate_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def generate_structured(
        self, messages, schema: type[BaseModel], *, temperature=0.0, max_tokens=1024
    ):
        self.generate_structured_calls.append((messages, schema))
        queue = self._structured_queue[schema]
        if not queue:
            raise LLMRequestError(
                f"scripted generate_structured({schema.__name__}) queue exhausted"
            )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, messages, *, temperature=0.0, max_tokens=1024) -> Iterator[str]:
        raise NotImplementedError
