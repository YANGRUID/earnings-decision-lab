"""Phase 4 RAG hardening (2026-08-26), Section 25 -- an OPTIONAL, local,
deterministic cross-encoder reranker for hybrid_search's own top-K
results. Never the default: rag/retrieval.py's own module docstring
already documents why RRF-only was the deliberate choice at this
project's scale, and this module doesn't overturn that decision --
Section 26's own gating principle applies here too ("only switch default
retrieval after V2 performs at least as well as V1 on objective
fixtures"; see evaluation/rag_fixtures.py). Reranking is opt-in
(``rerank=True`` on hybrid_search) until real fixture evidence justifies
turning it on by default.

Uses ``fastembed``'s own ``TextCrossEncoder`` -- fastembed is already a
real dependency (rag/embeddings.py's own FastEmbedProvider), so this adds
no new package, and it's local/ONNX-based like the embedding model, never
a second paid LLM call just to rerank (this project's own explicit
preference, see this phase's own final report).
"""

from abc import ABC, abstractmethod
from dataclasses import replace

from rag.retrieval import RetrievedChunk

# Xenova/ms-marco-MiniLM-L-6-v2: 0.08GB, the smallest of fastembed's
# supported cross-encoders -- proportionate to this project's real scale
# (a handful of tickers, a modest real filing count; see rag/retrieval.py's
# own docstring for the identical reasoning already applied to the choice
# not to add a reranker at all before now).
DEFAULT_RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
RERANKER_VERSION = "reranker-v1"

# Never reranks more than this many candidates -- Section 25's own
# "bounded to top-N candidates" requirement. A cross-encoder scores every
# (query, candidate) pair independently (unlike the embedding model,
# there's no batching shortcut across candidates), so this bounds real
# per-request latency regardless of how large ``k`` a caller requests.
MAX_RERANK_CANDIDATES = 20


class Reranker(ABC):
    model_name: str
    version: str

    @abstractmethod
    def score(self, query: str, documents: list[str]) -> list[float]:
        """One real relevance score per document, same order, higher is
        more relevant. Never raises for a normal (query, documents) pair
        -- see rerank_chunks's own try/except for the "fails safely"
        contract Section 44 asks for."""


class FastEmbedReranker(Reranker):
    model_name = DEFAULT_RERANKER_MODEL
    version = RERANKER_VERSION

    def __init__(self) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._model = TextCrossEncoder(model_name=self.model_name)

    def score(self, query: str, documents: list[str]) -> list[float]:
        return list(self._model.rerank(query, documents))


def rerank_chunks(
    reranker: Reranker,
    query: str,
    chunks: list[RetrievedChunk],
    *,
    top_n: int = MAX_RERANK_CANDIDATES,
) -> list[RetrievedChunk]:
    """Rescores the first ``min(top_n, len(chunks))`` chunks (already
    ranked by hybrid_search's own RRF) using the real cross-encoder,
    re-sorts them by that score (descending), and appends any chunks
    beyond ``top_n`` unchanged at the end -- reranking only ever
    reorders the bounded head of the list, never silently drops a
    candidate hybrid_search already returned.

    Fails safe (Section 44): if the reranker itself raises for any
    reason (e.g. a real model-loading failure), returns ``chunks``
    completely unchanged rather than raising -- a broken optional
    reranking layer must never break retrieval itself.
    """
    if not chunks:
        return chunks
    head = chunks[:top_n]
    tail = chunks[top_n:]
    try:
        scores = reranker.score(query, [c.text for c in head])
    except Exception:
        return chunks
    rescored = [
        replace(chunk, score=float(score)) for chunk, score in zip(head, scores, strict=True)
    ]
    rescored.sort(key=lambda c: c.score, reverse=True)
    return [*rescored, *tail]
