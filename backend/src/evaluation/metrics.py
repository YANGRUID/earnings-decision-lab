"""Pure, dependency-free metric functions for the evaluation framework.

Deliberately separated from the runner scripts (``evaluation/scripts/`` at
the repo root) that call live retrieval/RAG/agent/extraction code: these
functions take plain Python values in and return plain numbers out, so they
can be unit-tested without a database, an LLM provider, or any I/O. See
docs/evaluation.md for how each metric maps to what a runner script
measures and why substring/keyword matching is used for "correctness"
instead of an LLM judge.
"""

from collections.abc import Sequence


def recall_at_k(retrieved_ids: Sequence[int], relevant_ids: set[int], k: int) -> float:
    """Fraction of ``relevant_ids`` present in the top ``k`` of ``retrieved_ids``."""
    if not relevant_ids:
        raise ValueError("relevant_ids must be non-empty")
    top_k = set(retrieved_ids[:k])
    return len(top_k & relevant_ids) / len(relevant_ids)


def mean_reciprocal_rank(retrieved_ids: Sequence[int], relevant_ids: set[int]) -> float:
    """Reciprocal rank of the first relevant hit in ``retrieved_ids``, or 0.0 if none."""
    for rank, chunk_id in enumerate(retrieved_ids, start=1):
        if chunk_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def citation_precision(cited_ids: Sequence[int], relevant_ids: set[int]) -> float:
    """Fraction of ``cited_ids`` that are actually relevant.

    Returns 0.0 when nothing was cited — an unhelpful answer scores as
    imprecise, not as trivially perfect.
    """
    if not cited_ids:
        return 0.0
    hits = len(set(cited_ids) & relevant_ids)
    return hits / len(cited_ids)


def citation_completeness(cited_ids: Sequence[int], relevant_ids: set[int]) -> float:
    """Fraction of ``relevant_ids`` that were actually cited."""
    if not relevant_ids:
        raise ValueError("relevant_ids must be non-empty")
    hits = len(set(cited_ids) & relevant_ids)
    return hits / len(relevant_ids)


def fact_coverage(answer_text: str, required_facts: Sequence[str]) -> tuple[float, list[str]]:
    """Deterministic, case-insensitive substring check for whether ``answer_text``
    states each of ``required_facts``. Returns (coverage_fraction, missing_facts).

    This is intentionally a blunt instrument, not a semantic judge: a
    factually-correct answer that paraphrases a number's formatting (e.g.
    "$3.2B" instead of "$3.2 billion") registers as missing that fact. See
    docs/evaluation.md for why this tradeoff was chosen over an LLM judge as
    the primary correctness signal, and docs/limitations.md for the known
    false-negative risk.
    """
    if not required_facts:
        return 1.0, []
    lowered = answer_text.lower()
    missing = [fact for fact in required_facts if fact.lower() not in lowered]
    covered = len(required_facts) - len(missing)
    return covered / len(required_facts), missing
