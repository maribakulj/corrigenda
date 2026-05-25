"""Pin parity between parser._build_ocr_text and rewriter._extract_text_from_line.

Both functions reconstruct the logical text of a TextLine from its
String/SP/HYP children, applying the same soft-hyphen and double-dash
rules. They are independent implementations today (audit §6.2). Phase 3
intends to merge them into a single helper.

This test characterises the CURRENT relationship between the two
functions so that the merge is provably behaviour-preserving:
  - both return the same NFC-normalized payload;
  - the only documented difference is the parser's terminal `.strip()`.

If the merge drifts text reconstruction in any way, this test fails.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from app.alto._ns import _detect_namespace
from app.alto.parser import _build_ocr_text
from app.alto.rewriter import _extract_text_from_line

_EXAMPLES = Path(__file__).parent.parent.parent / "examples"
_SAMPLE_FILES = [_EXAMPLES / "sample.xml", _EXAMPLES / "X0000002.xml"]


def _iter_textlines(xml_path: Path):
    root = etree.parse(str(xml_path)).getroot()
    ns = _detect_namespace(root)
    tag = f"{{{ns}}}TextLine" if ns else "TextLine"
    for tl in root.iter(tag):
        yield tl, ns


def test_corpus_files_are_present():
    """Sanity: the audit fixtures we rely on must exist on disk."""
    for path in _SAMPLE_FILES:
        assert path.is_file(), f"Missing fixture: {path}"


def test_reconstructed_text_matches_modulo_strip():
    """For every TextLine in the corpus, the rewriter's reconstruction
    must equal the parser's reconstruction once both are stripped.

    The parser already strips (parser.py:46). The rewriter does not
    (rewriter.py:142). The shared bytes-in-between (NFC, soft-hyphen
    handling, HYP-after-dash skip) must be identical.
    """
    for xml_path in _SAMPLE_FILES:
        for tl, ns in _iter_textlines(xml_path):
            parser_text = _build_ocr_text(tl, ns)
            rewriter_text = _extract_text_from_line(tl, ns)
            assert parser_text == rewriter_text.strip(), (
                f"{xml_path.name}/{tl.get('ID')!r}: "
                f"parser={parser_text!r} rewriter.strip={rewriter_text.strip()!r}"
            )


def test_reconstructed_text_is_nfc_normalized():
    """Both reconstructors must return NFC text — pinning the contract
    the unified helper will inherit."""
    import unicodedata

    for xml_path in _SAMPLE_FILES:
        for tl, ns in _iter_textlines(xml_path):
            parser_text = _build_ocr_text(tl, ns)
            rewriter_text = _extract_text_from_line(tl, ns)
            assert parser_text == unicodedata.normalize("NFC", parser_text)
            assert rewriter_text == unicodedata.normalize("NFC", rewriter_text)


def test_soft_hyphen_in_hyp_normalized_to_dash_in_both():
    """When a HYP element carries U+00AD as CONTENT, both reconstructors
    must emit a regular '-' instead. (Soft-hyphen normalization is scoped
    to HYP only — String CONTENT is passed through verbatim.)"""
    ns = "http://www.loc.gov/standards/alto/ns-v3#"
    nsmap = {None: ns}
    tl = etree.Element(f"{{{ns}}}TextLine", nsmap=nsmap)
    s = etree.SubElement(tl, f"{{{ns}}}String")
    s.set("CONTENT", "fonda")
    h = etree.SubElement(tl, f"{{{ns}}}HYP")
    h.set("CONTENT", "­")

    parser_text = _build_ocr_text(tl, ns)
    rewriter_text = _extract_text_from_line(tl, ns)

    assert parser_text.endswith("-")
    assert "­" not in parser_text
    assert rewriter_text.endswith("-")
    assert "­" not in rewriter_text


def test_hyp_after_trailing_dash_skipped_in_both():
    """When CONTENT already ends with '-' the trailing HYP is skipped.
    Both functions implement this rule; pin it."""
    ns = "http://www.loc.gov/standards/alto/ns-v3#"
    nsmap = {None: ns}
    tl = etree.Element(f"{{{ns}}}TextLine", nsmap=nsmap)
    s = etree.SubElement(tl, f"{{{ns}}}String")
    s.set("CONTENT", "fonda-")
    h = etree.SubElement(tl, f"{{{ns}}}HYP")
    h.set("CONTENT", "-")

    parser_text = _build_ocr_text(tl, ns)
    rewriter_text = _extract_text_from_line(tl, ns)

    # Single trailing dash, not double
    assert parser_text.endswith("-")
    assert not parser_text.endswith("--")
    assert rewriter_text.endswith("-")
    assert not rewriter_text.endswith("--")
