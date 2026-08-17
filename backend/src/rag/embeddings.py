"""Embedding provider — abstracted the same way services.llm is, even
though only one implementation exists today.

No embeddings LLM key is configured or available: DeepSeek's official
model/pricing page lists only its two chat-completion models (no embeddings
endpoint, verified live against api-docs.deepseek.com); OPENAI_API_KEY
isn't configured; Anthropic doesn't offer embeddings directly (they
recommend a separate vendor, Voyage AI, which would mean yet another
account/key this project can't create autonomously). A local, ONNX-based
model via ``fastembed`` needs no API key or account, runs offline, and adds
a much smaller dependency footprint than a full PyTorch-based alternative
(e.g. sentence-transformers) — a real constraint on this development
machine. See docs/ai_architecture.md and docs/engineering_decisions.md.
"""

from abc import ABC, abstractmethod

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384


class EmbeddingProvider(ABC):
    model_name: str
    dimension: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """One embedding vector per input text, same order."""


class FastEmbedProvider(EmbeddingProvider):
    model_name = DEFAULT_MODEL_NAME
    dimension = EMBEDDING_DIM

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=self.model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]
