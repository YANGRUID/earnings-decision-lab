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


@dataclass(frozen=True)
class AssembledContext:
    context_text: str
    citations: list[Citation]


def assemble_context(chunks: list[RetrievedChunk]) -> AssembledContext:
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
            )
        )
        header = f"{marker} {chunk.ticker} {chunk.filing_type} filed {chunk.filing_date}"
        if chunk.section:
            header += f", {chunk.section}"
        parts.append(f"{header}\n{chunk.text}")
    return AssembledContext(context_text="\n\n".join(parts), citations=citations)
