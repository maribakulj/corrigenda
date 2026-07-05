"""Provenance stamped into the corrected XML's processingStep (spec §11)."""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from corrigenda.alto._ns import _detect_namespace
from corrigenda.alto.parser import build_document_manifest
from corrigenda.alto.rewriter import rewrite_alto_file

_NS = "http://www.loc.gov/standards/alto/ns-v4#"

# ALTO with a <Description><Processing> so the rewriter appends a
# processingStep (the sample corpus uses <OCRProcessing>, which the
# rewriter intentionally leaves alone).
_ALTO = f"""<?xml version="1.0"?>
<alto xmlns="{_NS}">
  <Description><Processing ID="P0"/></Description>
  <Layout>
    <Page ID="P1" WIDTH="600" HEIGHT="800">
      <PrintSpace>
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="30">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="30">
            <String ID="S1" CONTENT="bonjour" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="30"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def _write(tmp_path: Path) -> Path:
    p = tmp_path / "prov.xml"
    p.write_text(_ALTO, encoding="utf-8")
    return p


def _processing_step_descriptions(xml_bytes: bytes) -> list[str]:
    root = etree.fromstring(xml_bytes)
    ns = _detect_namespace(root)
    tag = f"{{{ns}}}processingStep" if ns else "processingStep"
    return [el.get("description", "") for el in root.iter(tag)]


def test_processing_step_carries_version_and_fingerprint(tmp_path: Path):
    xml_path = _write(tmp_path)
    doc = build_document_manifest([(xml_path, xml_path.name)])
    xml_bytes, _m, _p = rewrite_alto_file(
        xml_path,
        doc.pages,
        provider="openai",
        model="gpt-x",
        lib_version="9.9.9",
        config_fingerprint="deadbeefcafe0000",
    )
    descs = _processing_step_descriptions(xml_bytes)
    assert descs, "no processingStep written"
    assert any("openai/gpt-x" in d for d in descs)
    assert any("corrigenda 9.9.9" in d for d in descs)
    assert any("config deadbeefcafe0000" in d for d in descs)


def test_processing_step_backwards_compatible_without_provenance(tmp_path: Path):
    """Omitting version/fingerprint yields the historical description."""
    xml_path = _write(tmp_path)
    doc = build_document_manifest([(xml_path, xml_path.name)])
    xml_bytes, _m, _p = rewrite_alto_file(
        xml_path, doc.pages, provider="openai", model="gpt-x"
    )
    descs = _processing_step_descriptions(xml_bytes)
    assert any(d == "Post-OCR correction via openai/gpt-x (corrigenda)" for d in descs)
