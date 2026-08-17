from rag.parsing import html_to_text, split_into_sections

SAMPLE_HTML = """
<html><body>
<p>Cover page content.</p>
<p>Item 1. Business</p>
<p>We design semiconductor products.</p>
<p>Item 1A. Risk Factors</p>
<p>Our results may fluctuate due to industry cyclicality.</p>
<script>console.log('ignored')</script>
<style>.x { color: red; }</style>
<p>Item 7. Management's Discussion and Analysis</p>
<p>Revenue increased year over year.</p>
</body></html>
"""


def test_html_to_text_strips_script_and_style():
    text = html_to_text(SAMPLE_HTML)
    assert "console.log" not in text
    assert "color: red" not in text
    assert "We design semiconductor products." in text


def test_split_into_sections_detects_item_headings():
    text = html_to_text(SAMPLE_HTML)
    sections = split_into_sections(text)

    labels = [s.label for s in sections]
    assert any(label and label.startswith("Item 1 ") for label in labels)
    assert any(label and label.startswith("Item 1A") for label in labels)
    assert any(label and label.startswith("Item 7") for label in labels)


def test_split_into_sections_keeps_preamble_before_first_heading():
    text = html_to_text(SAMPLE_HTML)
    sections = split_into_sections(text)

    assert sections[0].label is None
    assert "Cover page content." in sections[0].text


def test_split_into_sections_assigns_body_text_correctly():
    text = html_to_text(SAMPLE_HTML)
    sections = split_into_sections(text)

    risk_section = next(s for s in sections if s.label and s.label.startswith("Item 1A"))
    assert "industry cyclicality" in risk_section.text
    assert "Revenue increased" not in risk_section.text


def test_split_into_sections_no_headings_returns_single_section():
    sections = split_into_sections("just some plain text with no item headings at all")
    assert len(sections) == 1
    assert sections[0].label is None
