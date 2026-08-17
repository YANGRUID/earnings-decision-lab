"""End-to-end question answering: hybrid retrieval -> context assembly ->
LLM synthesis, grounded and cited. This is the proof that the RAG pipeline
works end to end; full multi-tool agent orchestration (combining this with
the deterministic analytics tools) is Phase 7's job, not this module's.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from rag.context import Citation, assemble_context
from rag.embeddings import EmbeddingProvider
from rag.retrieval import RetrievalFilters, hybrid_search
from services.llm.base import LLMProvider
from services.llm.types import ChatMessage

SYSTEM_PROMPT = (
    "You are a research assistant answering questions about SEC filings using ONLY the "
    "provided context. Cite sources inline using the [N] markers exactly as given in the "
    "context. If the context does not contain the answer, say so explicitly rather than "
    "guessing or using outside knowledge."
)


@dataclass(frozen=True)
class AnsweredQuestion:
    question: str
    answer: str
    citations: list[Citation]
    retrieved_chunk_count: int


def answer_question(
    db: Session,
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    question: str,
    filters: RetrievalFilters | None = None,
    k: int = 6,
) -> AnsweredQuestion:
    query_embedding = embedder.embed([question])[0]
    chunks = hybrid_search(db, question, query_embedding, filters, k=k)
    assembled = assemble_context(chunks)

    if not chunks:
        return AnsweredQuestion(
            question=question,
            answer="No matching filing content was found for this question.",
            citations=[],
            retrieved_chunk_count=0,
        )

    messages = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=f"Context:\n{assembled.context_text}\n\nQuestion: {question}",
        ),
    ]
    result = llm.generate(messages, temperature=0.0, max_tokens=800)

    return AnsweredQuestion(
        question=question,
        answer=result.content or "",
        citations=assembled.citations,
        retrieved_chunk_count=len(chunks),
    )
