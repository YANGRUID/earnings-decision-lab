"""Assembles retrieved chunks into a prompt-ready context string plus a
structured citation list — the citation list is what a UI renders, not
anything parsed back out of the model's free-text answer.
"""

from dataclasses import dataclass
from datetime import date

from rag.retrieval import RetrievedChunk


@dataclass(frozen=True)
class Citation:
    marker: str
    ticker: str
    filing_type: str
    filing_date: date
    section: str | None
    source_url: str
    # Phase 4 AI Research source-transparency hardening (2026-08-26),
    # Section 28 -- real, already-known provenance, never fabricated:
    # accession_number is the real SEC filing identifier (None only for
    # a filing type that genuinely has none); evidence_cutoff is the
    # real as_of a historical/replay caller passed to retrieval (None
    # for ordinary "as of now" research, matching every other as_of
    # field's own None-means-unrestricted convention in this project).
    accession_number: str | None = None
    evidence_cutoff: date | None = None


@dataclass(frozen=True)
class AssembledContext:
    context_text: str
    citations: list[Citation]


def assemble_context(
    chunks: list[RetrievedChunk], *, evidence_cutoff: date | None = None
) -> AssembledContext:
    citations: list[Citation] = []
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        marker = f"[{i}]"
        citations.append(
            Citation(
                marker=marker,
                ticker=chunk.ticker,
                filing_type=chunk.filing_type,
                filing_date=chunk.filing_date,
                section=chunk.section,
                source_url=chunk.source_url,
                accession_number=chunk.accession_number,
                evidence_cutoff=evidence_cutoff,
            )
        )
        header = f"{marker} {chunk.ticker} {chunk.filing_type} filed {chunk.filing_date}"
        if chunk.section:
            header += f", {chunk.section}"
        parts.append(f"{header}\n{chunk.text}")
    return AssembledContext(context_text="\n\n".join(parts), citations=citations)
