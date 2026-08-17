import pytest

from rag.chunking import approximate_token_count, chunk_sections
from rag.parsing import Section


def test_approximate_token_count():
    assert approximate_token_count("one two three") == 3
    assert approximate_token_count("") == 0


def test_chunk_sections_single_small_section_no_split():
    sections = [Section(label="Item 1", text="a b c d e")]
    chunks = chunk_sections(sections, target_tokens=10, overlap_tokens=2)

    assert len(chunks) == 1
    assert chunks[0].text == "a b c d e"
    assert chunks[0].section == "Item 1"
    assert chunks[0].chunk_index == 0


def test_chunk_sections_splits_long_section_with_overlap():
    words = [f"w{i}" for i in range(25)]
    sections = [Section(label="Item 1A", text=" ".join(words))]

    chunks = chunk_sections(sections, target_tokens=10, overlap_tokens=3)

    # step = 7 words/chunk advance; starts at 0, 7, 14, 21 -> 4 chunks
    assert len(chunks) == 4
    assert all(c.section == "Item 1A" for c in chunks)
    # verify overlap: last 3 words of chunk 0 == first 3 words of chunk 1
    assert chunks[0].text.split()[-3:] == chunks[1].text.split()[:3]


def test_chunk_sections_never_spans_two_sections():
    sections = [Section(label="A", text="a b c"), Section(label="B", text="d e f")]
    chunks = chunk_sections(sections, target_tokens=10, overlap_tokens=2)

    assert len(chunks) == 2
    assert chunks[0].section == "A"
    assert chunks[1].section == "B"
    # chunk_index is continuous across sections
    assert [c.chunk_index for c in chunks] == [0, 1]


def test_chunk_sections_empty_section_produces_no_chunks():
    sections = [Section(label="Empty", text="")]
    assert chunk_sections(sections) == []


def test_chunk_sections_rejects_overlap_not_smaller_than_target():
    sections = [Section(label="A", text="a b c d e f g h i j")]
    with pytest.raises(ValueError):
        chunk_sections(sections, target_tokens=5, overlap_tokens=5)
