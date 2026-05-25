"""Pin the rewriter UNTOUCHED path (audit §6.1, §6.2).

When a TextLine's corrected text matches its OCR text and no SUBS
change is needed, `rewrite_alto_file` must take the UNTOUCHED path:
no children rewritten, no attribute reordered, no byte drift on the
TextLine element. The processingStep at the document root and the
overall pretty-print policy are out of scope here.

Phase 3 will fuse the three `_rebuild_*` functions and unify the
text-reconstruction helper. This test fails if either change leaks
into the UNTOUCHED path and silently rewrites a stable line.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from app.alto._ns import _detect_namespace
from app.alto.parser import build_document_manifest
from app.alto.rewriter import rewrite_alto_file

_EXAMPLES = Path(__file__).parent.parent.parent / "examples"
_SAMPLE_FILES = [_EXAMPLES / "sample.xml", _EXAMPLES / "X0000002.xml"]


def _set_identity_corrections(doc) -> None:
    """Mark every line as corrected with its own OCR text (no change)."""
    for page in doc.pages:
        for lm in page.lines:
            lm.corrected_text = lm.ocr_text


def _textlines_by_id(root) -> dict:
    ns = _detect_namespace(root)
    tag = f"{{{ns}}}TextLine" if ns else "TextLine"
    return {tl.get("ID"): tl for tl in root.iter(tag)}


def _local(child) -> str:
    return etree.QName(child.tag).localname


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------


def test_identity_corrections_take_untouched_or_subs_only_paths():
    """Every line must classify as either UNTOUCHED or SUBS_ONLY.
    The SLOW path or FAST path appearing here would mean the rewriter
    perturbed a line whose text hasn't changed."""
    for xml_path in _SAMPLE_FILES:
        doc = build_document_manifest([(xml_path, xml_path.name)])
        _set_identity_corrections(doc)

        _bytes, metrics, paths = rewrite_alto_file(
            xml_path, doc.pages, provider="test", model="mock"
        )

        unexpected = {lid: p for lid, p in paths.items() if p not in ("untouched", "subs_only")}
        assert not unexpected, (
            f"{xml_path.name}: lines classified as fast/slow despite identity correction: "
            f"{unexpected}"
        )
        assert metrics.fast_path == 0
        assert metrics.slow_path == 0


# ---------------------------------------------------------------------------
# String CONTENT byte stability
# ---------------------------------------------------------------------------


def test_untouched_lines_preserve_string_content_verbatim():
    """For UNTOUCHED lines, each String CONTENT attribute must be
    byte-identical to the source."""
    for xml_path in _SAMPLE_FILES:
        doc = build_document_manifest([(xml_path, xml_path.name)])
        _set_identity_corrections(doc)

        xml_bytes, _metrics, paths = rewrite_alto_file(
            xml_path, doc.pages, provider="test", model="mock"
        )

        orig_root = etree.parse(str(xml_path)).getroot()
        new_root = etree.fromstring(xml_bytes)
        orig_by_id = _textlines_by_id(orig_root)
        new_by_id = _textlines_by_id(new_root)

        for line_id, classification in paths.items():
            if classification != "untouched":
                continue
            orig_contents = [c.get("CONTENT") for c in orig_by_id[line_id] if _local(c) == "String"]
            new_contents = [c.get("CONTENT") for c in new_by_id[line_id] if _local(c) == "String"]
            assert orig_contents == new_contents, (
                f"{xml_path.name}/{line_id}: CONTENT changed on UNTOUCHED path: "
                f"{orig_contents} -> {new_contents}"
            )


# ---------------------------------------------------------------------------
# Geometry stability (HPOS, VPOS, WIDTH, HEIGHT) on String / SP / HYP
# ---------------------------------------------------------------------------


def test_untouched_lines_preserve_child_geometry():
    """UNTOUCHED lines must preserve every child element's geometry
    attributes (HPOS/VPOS/WIDTH/HEIGHT) byte-identically — this is the
    primary invariant the rewriter docstring promises."""
    geom_attrs = ("HPOS", "VPOS", "WIDTH", "HEIGHT")
    for xml_path in _SAMPLE_FILES:
        doc = build_document_manifest([(xml_path, xml_path.name)])
        _set_identity_corrections(doc)

        xml_bytes, _metrics, paths = rewrite_alto_file(
            xml_path, doc.pages, provider="test", model="mock"
        )

        orig_root = etree.parse(str(xml_path)).getroot()
        new_root = etree.fromstring(xml_bytes)
        orig_by_id = _textlines_by_id(orig_root)
        new_by_id = _textlines_by_id(new_root)

        for line_id, classification in paths.items():
            if classification != "untouched":
                continue
            orig_tl = orig_by_id[line_id]
            new_tl = new_by_id[line_id]

            def geom_seq(tl):
                return [
                    (_local(c), tuple(c.get(a) for a in geom_attrs))
                    for c in tl
                    if _local(c) in ("String", "SP", "HYP")
                ]

            assert geom_seq(orig_tl) == geom_seq(new_tl), (
                f"{xml_path.name}/{line_id}: child geometry changed on UNTOUCHED path"
            )


# ---------------------------------------------------------------------------
# TextLine attributes (ID, HPOS, VPOS, WIDTH, HEIGHT) untouched on every path
# ---------------------------------------------------------------------------


def test_textline_own_attributes_never_change():
    """ALTO geometry rule: the rewriter never modifies TextLine's own
    attributes. Holds for UNTOUCHED, SUBS_ONLY, FAST and SLOW alike."""
    for xml_path in _SAMPLE_FILES:
        doc = build_document_manifest([(xml_path, xml_path.name)])
        _set_identity_corrections(doc)

        xml_bytes, _metrics, _paths = rewrite_alto_file(
            xml_path, doc.pages, provider="test", model="mock"
        )

        orig_root = etree.parse(str(xml_path)).getroot()
        new_root = etree.fromstring(xml_bytes)
        orig_by_id = _textlines_by_id(orig_root)
        new_by_id = _textlines_by_id(new_root)

        for line_id, orig_tl in orig_by_id.items():
            new_tl = new_by_id[line_id]
            for attr in ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT"):
                assert orig_tl.get(attr) == new_tl.get(attr), (
                    f"{xml_path.name}/{line_id}: TextLine attr {attr} changed: "
                    f"{orig_tl.get(attr)!r} -> {new_tl.get(attr)!r}"
                )
