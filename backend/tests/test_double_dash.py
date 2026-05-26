"""Sprint 4 — Double-dash / soft-hyphen normalization non-regression tests.

Proves:
  1. CONTENT="xxx-" + HYP CONTENT="-" → single dash in ocr_text (not "xxx--")
  2. CONTENT="xxx" + HYP CONTENT="\xad" → normalized to "xxx-" (not "xxx\xad")
  3. CONTENT="xxx" + HYP CONTENT="-" → normal "xxx-" (no-op, baseline)
  4. Hyphen pairs still correctly detected after normalization
  5. Rewriter round-trip preserves XML structure
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alto_core.alto.parser import parse_alto_file
from alto_core.alto.rewriter import rewrite_alto_file
from lxml import etree

from app.schemas import HyphenRole

NS = "http://www.loc.gov/standards/alto/ns-v3#"


def _ns(local: str) -> str:
    return f"{{{NS}}}{local}"


def _write(tmp_path: Path, name: str, xml: str) -> Path:
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_alto(
    part1_content: str, hyp_content: str, part2_content: str, subs_content: str = "dénonçait"
) -> str:
    """Build minimal ALTO with PART1 (String + HYP) and PART2."""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="{NS}">
  <Description>
    <MeasurementUnit>pixel</MeasurementUnit>
    <OCRProcessing ID="OCR_1"><ocrProcessingStep/></OCRProcessing>
    <Processing/>
  </Description>
  <Layout>
    <Page ID="P1" WIDTH="2480" HEIGHT="3508">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="2480" HEIGHT="3508">
        <TextBlock ID="TB1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="200">
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="{part1_content}" HPOS="100" VPOS="100"
                    WIDTH="200" HEIGHT="50"
                    SUBS_TYPE="HypPart1" SUBS_CONTENT="{subs_content}"/>
            <HYP CONTENT="{hyp_content}"/>
          </TextLine>
          <TextLine ID="TL2" HPOS="100" VPOS="180" WIDTH="2000" HEIGHT="60">
            <String ID="S2" CONTENT="{part2_content}" HPOS="100" VPOS="180"
                    WIDTH="200" HEIGHT="50"
                    SUBS_TYPE="HypPart2" SUBS_CONTENT="{subs_content}"/>
            <SP WIDTH="20"/>
            <String ID="S3" CONTENT="les" HPOS="320" VPOS="180"
                    WIDTH="80" HEIGHT="50"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


# ===========================================================================
# Test 1: CONTENT="dénon-" + HYP="-" → "dénon-" (not "dénon--")
# ===========================================================================


class TestContentDashPlusHypDash:
    """CONTENT already has trailing dash + HYP adds another → single dash."""

    def test_ocr_text_single_dash(self, tmp_path):
        xml = _make_alto("dénon-", "-", "çait")
        path = _write(tmp_path, "dd.xml", xml)
        pages, _ = parse_alto_file(path, "dd.xml")

        p1 = pages[0].lines[0]
        assert p1.ocr_text == "dénon-", f"got {p1.ocr_text!r}"
        assert "--" not in p1.ocr_text

    def test_hyphen_pair_detected(self, tmp_path):
        xml = _make_alto("dénon-", "-", "çait")
        path = _write(tmp_path, "dd.xml", xml)
        pages, _ = parse_alto_file(path, "dd.xml")

        p1 = pages[0].lines[0]
        p2 = pages[0].lines[1]
        assert p1.hyphen_role == HyphenRole.PART1
        assert p2.hyphen_role == HyphenRole.PART2
        assert p1.hyphen_pair_line_id == p2.line_id
        assert p1.hyphen_subs_content == "dénonçait"

    def test_rewriter_round_trip(self, tmp_path):
        xml = _make_alto("dénon-", "-", "çait")
        path = _write(tmp_path, "dd.xml", xml)
        pages, _ = parse_alto_file(path, "dd.xml")

        xml_bytes, metrics, _paths = rewrite_alto_file(path, pages, "test", "model")
        assert metrics.untouched == 2
        root = etree.fromstring(xml_bytes)
        hyps = root.findall(f".//{_ns('HYP')}")
        assert len(hyps) == 1
        assert hyps[0].get("CONTENT") == "-"


# ===========================================================================
# Test 2: CONTENT="xxx" + HYP="\xad" → "xxx-" (normalized)
# ===========================================================================


class TestContentPlusSoftHyphen:
    """CONTENT without dash + HYP=soft-hyphen → normalized to single dash."""

    def test_ocr_text_normalized(self, tmp_path):
        xml = _make_alto("dénon", "\u00ad", "çait")
        path = _write(tmp_path, "sh.xml", xml)
        pages, _ = parse_alto_file(path, "sh.xml")

        p1 = pages[0].lines[0]
        assert p1.ocr_text == "dénon-", f"got {p1.ocr_text!r}"
        assert "\u00ad" not in p1.ocr_text

    def test_hyphen_pair_detected(self, tmp_path):
        xml = _make_alto("dénon", "\u00ad", "çait")
        path = _write(tmp_path, "sh.xml", xml)
        pages, _ = parse_alto_file(path, "sh.xml")

        p1 = pages[0].lines[0]
        assert p1.hyphen_role == HyphenRole.PART1
        assert p1.hyphen_source_explicit is True


# ===========================================================================
# Test 3: CONTENT="xxx" + HYP="-" → "xxx-" (baseline, no change)
# ===========================================================================


class TestContentPlusHypDash:
    """Standard ALTO: CONTENT without dash + HYP="-" → "xxx-"."""

    def test_ocr_text_standard(self, tmp_path):
        xml = _make_alto("dénon", "-", "çait")
        path = _write(tmp_path, "std.xml", xml)
        pages, _ = parse_alto_file(path, "std.xml")

        p1 = pages[0].lines[0]
        assert p1.ocr_text == "dénon-"

    def test_hyphen_pair_detected(self, tmp_path):
        xml = _make_alto("dénon", "-", "çait")
        path = _write(tmp_path, "std.xml", xml)
        pages, _ = parse_alto_file(path, "std.xml")

        p1 = pages[0].lines[0]
        p2 = pages[0].lines[1]
        assert p1.hyphen_role == HyphenRole.PART1
        assert p2.hyphen_role == HyphenRole.PART2


# ===========================================================================
# Test 4: sample.xml regression — no double dash after fix
# ===========================================================================

SAMPLE_PATH = Path(__file__).resolve().parent.parent.parent / "examples" / "sample.xml"


@pytest.mark.skipif(not SAMPLE_PATH.exists(), reason="sample.xml not found")
class TestSampleXmlDoubleDash:
    """Verify sample.xml no longer produces double-dash in ocr_text."""

    def test_no_double_dash(self):
        pages, _ = parse_alto_file(SAMPLE_PATH, "sample.xml")
        for pg in pages:
            for lm in pg.lines:
                if lm.hyphen_role == HyphenRole.PART1:
                    stripped = lm.ocr_text.rstrip()
                    assert not stripped.endswith("--"), (
                        f"{lm.line_id}: double dash in {lm.ocr_text!r}"
                    )

    def test_denon_single_dash(self):
        pages, _ = parse_alto_file(SAMPLE_PATH, "sample.xml")
        lines = {lm.line_id: lm for pg in pages for lm in pg.lines}
        tl4 = lines["TL4"]
        assert tl4.ocr_text.endswith("dénon-"), f"got {tl4.ocr_text!r}"

    def test_fonda_single_dash(self):
        pages, _ = parse_alto_file(SAMPLE_PATH, "sample.xml")
        lines = {lm.line_id: lm for pg in pages for lm in pg.lines}
        tl8 = lines["TL8"]
        assert tl8.ocr_text.endswith("fonda-"), f"got {tl8.ocr_text!r}"
