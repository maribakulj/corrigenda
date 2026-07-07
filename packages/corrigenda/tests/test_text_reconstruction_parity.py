"""Pin parity between parser._build_ocr_text and reconstruct_textline.

Both call sites reconstruct the logical text of a TextLine from its
String/SP/HYP children. After the audit §6.2 fix they share the same
helper (``corrigenda.formats.alto._text.reconstruct_textline``); the parser
wraps it with ``.replace("\\r", "").strip()`` to get its "logical"
form, the rewriter calls it raw for byte-faithful UNTOUCHED detection.

This test pins the relationship between the two:
  - both return the same NFC-normalised payload;
  - the only documented difference is the parser's terminal `.strip()`.

If a future change drifts text reconstruction in any way, this test
fails.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from corrigenda.formats.alto._ns import _detect_namespace
from corrigenda.formats.alto._text import reconstruct_textline
from corrigenda.formats.alto.parser import _build_ocr_text
from lxml import etree

_EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples"
_SAMPLE_FILES = [_EXAMPLES / "sample.xml", _EXAMPLES / "X0000002.xml"]


def _iter_textlines(xml_path: Path):
    root = etree.parse(str(xml_path)).getroot()
    ns = _detect_namespace(root)
    tag = f"{{{ns}}}TextLine" if ns else "TextLine"
    for tl in root.iter(tag):
        yield tl, ns


def test_corpus_files_are_present():
    """Sanity: fixtures used by the parity assertions must exist."""
    for path in _SAMPLE_FILES:
        assert path.is_file(), f"Missing fixture: {path}"


def test_reconstructed_text_matches_modulo_strip():
    """For every TextLine in the corpus, the rewriter's reconstruction
    must equal the parser's once both are stripped.

    The parser already strips (parser.py terminal `.strip()`). The
    rewriter does not. The shared logic between them (NFC,
    soft-hyphen handling, HYP-after-dash skip) must be byte-identical.
    """
    for xml_path in _SAMPLE_FILES:
        for tl, ns in _iter_textlines(xml_path):
            parser_text = _build_ocr_text(tl, ns)
            rewriter_text = reconstruct_textline(tl, ns)
            assert parser_text == rewriter_text.strip(), (
                f"{xml_path.name}/{tl.get('ID')!r}: "
                f"parser={parser_text!r} rewriter.strip={rewriter_text.strip()!r}"
            )


def test_reconstructed_text_is_nfc_normalized():
    """Both reconstructors must return NFC text — pinning the contract
    a future unified helper will inherit."""
    for xml_path in _SAMPLE_FILES:
        for tl, ns in _iter_textlines(xml_path):
            parser_text = _build_ocr_text(tl, ns)
            rewriter_text = reconstruct_textline(tl, ns)
            assert parser_text == unicodedata.normalize("NFC", parser_text)
            assert rewriter_text == unicodedata.normalize("NFC", rewriter_text)


def test_soft_hyphen_in_hyp_normalized_to_dash_in_both():
    """When a HYP element carries U+00AD as CONTENT, both reconstructors
    must emit a regular '-' instead. (Soft-hyphen normalisation is
    scoped to HYP only — String CONTENT is passed through verbatim.)"""
    ns = "http://www.loc.gov/standards/alto/ns-v3#"
    nsmap = {None: ns}
    tl = etree.Element(f"{{{ns}}}TextLine", nsmap=nsmap)
    s = etree.SubElement(tl, f"{{{ns}}}String")
    s.set("CONTENT", "fonda")
    h = etree.SubElement(tl, f"{{{ns}}}HYP")
    h.set("CONTENT", "­")

    parser_text = _build_ocr_text(tl, ns)
    rewriter_text = reconstruct_textline(tl, ns)

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
    rewriter_text = reconstruct_textline(tl, ns)

    assert parser_text.endswith("-")
    assert not parser_text.endswith("--")
    assert rewriter_text.endswith("-")
    assert not rewriter_text.endswith("--")
