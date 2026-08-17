from datetime import date

from rag.context import assemble_context
from rag.retrieval import RetrievedChunk


def _chunk(ticker="MU", section="Item 7", text="Revenue grew.") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=1,
        filing_id=1,
        company_id=1,
        ticker=ticker,
        filing_type="10-Q",
        filing_date=date(2025, 12, 18),
        source_url="https://example.com/filing.htm",
        section=section,
        chunk_index=0,
        text=text,
        score=0.9,
    )


def test_assemble_context_numbers_citations_in_order():
    chunks = [_chunk(text="first"), _chunk(text="second"), _chunk(text="third")]

    assembled = assemble_context(chunks)

    assert [c.marker for c in assembled.citations] == ["[1]", "[2]", "[3]"]
    assert "[1]" in assembled.context_text
    assert "first" in assembled.context_text
    assert "third" in assembled.context_text


def test_assemble_context_includes_provenance_in_header():
    assembled = assemble_context([_chunk(ticker="NVDA", section="Item 1A")])

    assert "NVDA" in assembled.context_text
    assert "10-Q" in assembled.context_text
    assert "2025-12-18" in assembled.context_text
    assert "Item 1A" in assembled.context_text


def test_assemble_context_handles_no_section_label():
    assembled = assemble_context([_chunk(section=None)])
    # header should still be well-formed without a trailing ", None"
    assert ", None" not in assembled.context_text


def test_assemble_context_empty_input():
    assembled = assemble_context([])
    assert assembled.context_text == ""
    assert assembled.citations == []
