"""SEC filing HTML -> clean, section-aware text.

Replaces Phase 1's naive regex tag-stripper (still used by
``SECEdgarProvider.get_filing_text`` for the quick provenance check that
predates this module) with real HTML parsing via BeautifulSoup, plus
section detection on the standard "Item N." headings SEC filings use.

Section detection is regex-based pattern matching on cleaned text, not a
structural HTML parse of SEC's own semantic markup (real filings are
inconsistent enough across companies and years — different HTML generators,
inline vs. separate style, table-based vs. div-based layouts — that a fully
structural parser is a much larger undertaking than this project's scope
justifies). This means section boundaries are a best-effort classification,
not guaranteed 100% accurate on every filing — see docs/ai_architecture.md.
"""

import re

from bs4 import BeautifulSoup

# Matches "Item 1A.", "Item 7.", "ITEM 2.", optionally followed by a title
# on the same line — the standard SEC Item-numbering convention shared by
# 10-K, 10-Q, and 8-K filings.
_ITEM_HEADING_RE = re.compile(
    r"^\s*item\s+(\d{1,2}[a-c]?)\.?\s*[-–—.]?\s*(.{0,120})?$",
    re.IGNORECASE,
)
_MAX_HEADING_LINE_CHARS = 160  # a real heading is short; a false positive mid-paragraph is not


def html_to_text(html: str) -> str:
    """Real DOM-aware extraction: drops script/style, converts block
    elements to newlines so paragraph/table structure survives as line
    breaks, collapses excess whitespace.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


class Section:
    __slots__ = ("label", "text")

    def __init__(self, label: str | None, text: str) -> None:
        self.label = label
        self.text = text

    def __repr__(self) -> str:
        return f"Section(label={self.label!r}, {len(self.text)} chars)"


def split_into_sections(text: str) -> list[Section]:
    """Split cleaned text at "Item N." headings. Text before the first
    detected heading (cover page, TOC) becomes a section with ``label=None``
    rather than being dropped.
    """
    lines = text.splitlines()
    boundaries: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if len(line) > _MAX_HEADING_LINE_CHARS:
            continue
        match = _ITEM_HEADING_RE.match(line)
        if match:
            item_number = match.group(1).upper()
            title = (match.group(2) or "").strip()
            label = f"Item {item_number}" + (f" - {title}" if title else "")
            boundaries.append((i, label))

    if not boundaries:
        return [Section(label=None, text=text)]

    sections: list[Section] = []
    if boundaries[0][0] > 0:
        preamble = "\n".join(lines[: boundaries[0][0]]).strip()
        if preamble:
            sections.append(Section(label=None, text=preamble))

    for idx, (start_line, label) in enumerate(boundaries):
        end_line = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        body = "\n".join(lines[start_line:end_line]).strip()
        if body:
            sections.append(Section(label=label, text=body))

    return sections
