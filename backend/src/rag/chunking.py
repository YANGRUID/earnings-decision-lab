"""Section-aware, token-approximate sliding-window chunker.

"Token count" here is a whitespace-word count, not an exact tokenization
for any particular vendor's tokenizer (DeepSeek/OpenAI/Anthropic all
tokenize slightly differently, and this project is provider-agnostic by
design — see docs/llm_providers.md). It's a deliberate, documented
approximation good enough for sizing chunks consistently; exact token
accounting for cost/context-window purposes should use the configured
provider's own count where it matters (e.g. right before a request).
"""

from dataclasses import dataclass

from rag.parsing import Section

DEFAULT_TARGET_TOKENS = 400
DEFAULT_OVERLAP_TOKENS = 50


@dataclass(frozen=True)
class Chunk:
    section: str | None
    chunk_index: int
    text: str
    token_count: int


def approximate_token_count(text: str) -> int:
    return len(text.split())


def _chunk_words(words: list[str], target_tokens: int, overlap_tokens: int) -> list[list[str]]:
    if not words:
        return []
    if overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be smaller than target_tokens")
    step = target_tokens - overlap_tokens
    chunks = []
    start = 0
    while start < len(words):
        chunks.append(words[start : start + target_tokens])
        if start + target_tokens >= len(words):
            break
        start += step
    return chunks


def chunk_sections(
    sections: list[Section],
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Chunks within a section only — a chunk never spans two sections, so
    every chunk's ``section`` label is unambiguous for citation purposes.
    """
    chunks: list[Chunk] = []
    index = 0
    for section in sections:
        for word_group in _chunk_words(section.text.split(), target_tokens, overlap_tokens):
            text = " ".join(word_group)
            chunks.append(
                Chunk(
                    section=section.label,
                    chunk_index=index,
                    text=text,
                    token_count=len(word_group),
                )
            )
            index += 1
    return chunks
