"""Rewriter corrections F2 (drop stale WC/CC) and F4 (stripped UNTOUCHED).

F4 — the UNTOUCHED comparison strips both sides, so a line whose XML
reconstructs with a trailing space (a trailing ``<SP/>``) but whose
corrected text equals the stripped ``ocr_text`` takes the UNTOUCHED path
instead of being needlessly rewritten.

F2 — a changed CONTENT drops the now-stale WC/CC confidences.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from alto_core.alto._ns import _detect_namespace
from alto_core.alto.parser import build_document_manifest
from alto_core.alto.rewriter import rewrite_alto_file

_NS = "http://www.loc.gov/standards/alto/ns-v4#"


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "f2f4.xml"
    p.write_text(
        f'<?xml version="1.0"?>\n<alto xmlns="{_NS}"><Layout><Page ID="P1" '
        f'WIDTH="1000" HEIGHT="1000"><PrintSpace>{body}</PrintSpace></Page>'
        f"</Layout></alto>",
        encoding="utf-8",
    )
    return p


def _strings(xml_bytes: bytes, line_id: str) -> list[etree._Element]:
    root = etree.fromstring(xml_bytes)
    ns = _detect_namespace(root)
    tl = root.find(f".//{{{ns}}}TextLine[@ID='{line_id}']")
    assert tl is not None
    return [c for c in tl if c.tag == f"{{{ns}}}String"]


def test_f4_trailing_sp_line_is_untouched(tmp_path: Path):
    """A line ending in a trailing <SP/> reconstructs to 'mot ' but its
    ocr_text is the stripped 'mot'. With identity correction it must take
    the UNTOUCHED path (F4), not fast/slow."""
    body = (
        '<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="40">'
        '<TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="40">'
        '<String ID="S1" CONTENT="mot" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="40"/>'
        '<SP WIDTH="20" HPOS="100" VPOS="0"/>'
        "</TextLine></TextBlock>"
    )
    xml_path = _write(tmp_path, body)
    doc = build_document_manifest([(xml_path, xml_path.name)])
    # Identity correction — corrected text equals the (stripped) ocr_text.
    for page in doc.pages:
        for lm in page.lines:
            lm.corrected_text = lm.ocr_text

    _bytes, metrics, paths = rewrite_alto_file(
        xml_path, doc.pages, provider="t", model="m"
    )
    assert paths["L1"] == "untouched"
    assert metrics.fast_path == 0
    assert metrics.slow_path == 0


def test_f2_fast_path_drops_wc_cc_on_changed_content(tmp_path: Path):
    body = (
        '<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="40">'
        '<TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="40">'
        '<String ID="S1" CONTENT="Helo" HPOS="0" VPOS="0" WIDTH="240" HEIGHT="40"'
        ' WC="0.7" CC="0900" STYLEREFS="f1"/>'
        '<SP WIDTH="20" HPOS="240" VPOS="0"/>'
        '<String ID="S2" CONTENT="wrld" HPOS="260" VPOS="0" WIDTH="240" HEIGHT="40"'
        ' WC="0.6" CC="9090"/>'
        "</TextLine></TextBlock>"
    )
    xml_path = _write(tmp_path, body)
    doc = build_document_manifest([(xml_path, xml_path.name)])
    for page in doc.pages:
        for lm in page.lines:
            lm.corrected_text = "Hello world"

    xml_bytes, metrics, paths = rewrite_alto_file(
        xml_path, doc.pages, provider="t", model="m"
    )
    assert paths["L1"] == "fast_path"
    s = _strings(xml_bytes, "L1")
    assert s[0].get("CONTENT") == "Hello"
    assert s[0].get("WC") is None
    assert s[0].get("CC") is None
    assert s[0].get("STYLEREFS") == "f1"  # style preserved
    assert s[1].get("WC") is None
    assert s[1].get("CC") is None


def test_slow_path_sp_geometry_is_recomputed_not_recycled(tmp_path: Path):
    """Post-audit §6.1 fix — slow-path SPs must carry geometry from the
    same _compute_geometry pass as the surrounding Strings, not the stale
    pre-correction HPOS/WIDTH. Pin: the SP sits exactly between its
    neighbouring Strings (contiguous cursor), not at its old position."""
    body = (
        '<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="40">'
        '<TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="40">'
        '<String ID="S1" CONTENT="un" HPOS="0" VPOS="0" WIDTH="240" HEIGHT="40"/>'
        '<SP WIDTH="99" HPOS="777" VPOS="0"/>'
        '<String ID="S2" CONTENT="mot" HPOS="260" VPOS="0" WIDTH="240" HEIGHT="40"/>'
        "</TextLine></TextBlock>"
    )
    xml_path = _write(tmp_path, body)
    doc = build_document_manifest([(xml_path, xml_path.name)])
    for page in doc.pages:
        for lm in page.lines:
            lm.corrected_text = "un petit mot"  # 2 -> 3 words: slow path

    xml_bytes, _metrics, paths = rewrite_alto_file(
        xml_path, doc.pages, provider="t", model="m"
    )
    assert paths["L1"] == "slow_path"

    root = etree.fromstring(xml_bytes)
    ns = _detect_namespace(root)
    tl = root.find(f".//{{{ns}}}TextLine[@ID='L1']")
    assert tl is not None
    children = [c for c in tl if isinstance(c.tag, str)]

    cursor = 0
    for c in children:
        local = etree.QName(c.tag).localname
        if local not in ("String", "SP"):
            continue
        hpos = int(c.get("HPOS", "-1"))
        width = int(c.get("WIDTH", "-1"))
        assert hpos == cursor, f"{local} at HPOS={hpos}, expected {cursor}"
        assert width >= 1
        cursor += width
    # The stale SP position (777) must be gone.
    sps = [c for c in children if etree.QName(c.tag).localname == "SP"]
    assert sps and all(sp.get("HPOS") != "777" for sp in sps)
