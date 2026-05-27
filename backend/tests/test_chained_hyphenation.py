"""Sprint 5 — Chained hyphenation tests + diagnostic traces.

Tests:
  - Simple PART1-only and PART2-only lines
  - Chained hyphenation (BOTH role): parsing, linking, reconciliation, rewriting
  - Real X0000002.xml chained zone (pratica-/bles./des-/servent)
  - Diagnostic text traces: source OCR → model corrected → projected → output ALTO
"""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from pathlib import Path

import pytest
from alto_core.alto.hyphenation import (
    reconcile_hyphen_pair,
)
from alto_core.alto._text import reconstruct_textline
from alto_core.alto.parser import parse_alto_file
from alto_core.alto.rewriter import rewrite_alto_file
from lxml import etree

from app.schemas import HyphenRole

NS = "http://www.loc.gov/standards/alto/ns-v3#"


def _ns(local: str) -> str:
    return f"{{{NS}}}{local}"


# ---------------------------------------------------------------------------
# Diagnostic trace dataclass
# ---------------------------------------------------------------------------


@dataclass
class LineTrace:
    """Diagnostic trace for a single line through the pipeline."""

    line_id: str
    source_ocr_text: str  # text extracted from ALTO source
    model_corrected_text: str  # text returned by LLM (simulated)
    projected_text: str  # text retained after reconciliation, before rewrite
    output_alto_text: str  # text re-extracted from output ALTO


# ---------------------------------------------------------------------------
# ALTO fixture builder
# ---------------------------------------------------------------------------


def _alto_xml(textlines_xml: str) -> str:
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
        <TextBlock ID="TB1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="600">
{textlines_xml}
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def _write_fixture(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_output_text(root, line_id: str) -> str:
    """Re-extract text from output ALTO for a given line."""
    tl = root.find(f".//{_ns('TextLine')}[@ID='{line_id}']")
    if tl is None:
        return ""
    return reconstruct_textline(tl, NS)


def _get_subs(root, line_id: str, position: str) -> tuple[str | None, str | None]:
    tl = root.find(f".//{_ns('TextLine')}[@ID='{line_id}']")
    if tl is None:
        return None, None
    strings = [c for c in tl if c.tag == _ns("String")]
    if not strings:
        return None, None
    target = strings[-1] if position == "last" else strings[0]
    return target.get("SUBS_TYPE"), target.get("SUBS_CONTENT")


# ===========================================================================
# FIXTURE: Chained hyphenation — pratica-/bles. ... des-/servent
# ===========================================================================

CHAINED_XML = _alto_xml("""\
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="rendre" HPOS="100" VPOS="100" WIDTH="180" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S2" CONTENT="pratica" HPOS="300" VPOS="100" WIDTH="200" HEIGHT="50"
                    SUBS_TYPE="HypPart1" SUBS_CONTENT="praticables."/>
            <HYP CONTENT="-"/>
          </TextLine>
          <TextLine ID="TL2" HPOS="100" VPOS="180" WIDTH="2000" HEIGHT="60">
            <String ID="S3" CONTENT="bles." HPOS="100" VPOS="180" WIDTH="140" HEIGHT="50"
                    SUBS_TYPE="HypPart2" SUBS_CONTENT="praticables."/>
            <SP WIDTH="20"/>
            <String ID="S4" CONTENT="Les" HPOS="260" VPOS="180" WIDTH="80" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S5" CONTENT="chemins" HPOS="360" VPOS="180" WIDTH="200" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S6" CONTENT="des" HPOS="580" VPOS="180" WIDTH="90" HEIGHT="50"
                    SUBS_TYPE="HypPart1" SUBS_CONTENT="desservent"/>
            <HYP CONTENT="-"/>
          </TextLine>
          <TextLine ID="TL3" HPOS="100" VPOS="260" WIDTH="2000" HEIGHT="60">
            <String ID="S7" CONTENT="servent" HPOS="100" VPOS="260" WIDTH="200" HEIGHT="50"
                    SUBS_TYPE="HypPart2" SUBS_CONTENT="desservent"/>
            <SP WIDTH="20"/>
            <String ID="S8" CONTENT="bien." HPOS="320" VPOS="260" WIDTH="130" HEIGHT="50"/>
          </TextLine>""")


# ===========================================================================
# Test 1: Simple PART1-only
# ===========================================================================


class TestSimplePart1:
    def test_part1_only_detected(self, tmp_path):
        xml = _alto_xml("""\
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="rendre" HPOS="100" VPOS="100" WIDTH="180" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S2" CONTENT="pratica" HPOS="300" VPOS="100" WIDTH="200" HEIGHT="50"
                    SUBS_TYPE="HypPart1" SUBS_CONTENT="praticables"/>
            <HYP CONTENT="-"/>
          </TextLine>
          <TextLine ID="TL2" HPOS="100" VPOS="180" WIDTH="2000" HEIGHT="60">
            <String ID="S3" CONTENT="bles" HPOS="100" VPOS="180" WIDTH="140" HEIGHT="50"
                    SUBS_TYPE="HypPart2" SUBS_CONTENT="praticables"/>
            <SP WIDTH="20"/>
            <String ID="S4" CONTENT="ici." HPOS="260" VPOS="180" WIDTH="100" HEIGHT="50"/>
          </TextLine>""")
        path = _write_fixture(tmp_path, "p1.xml", xml)
        pages, _ = parse_alto_file(path, "p1.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        assert lines["TL1"].hyphen_role == HyphenRole.PART1
        assert lines["TL2"].hyphen_role == HyphenRole.PART2
        assert lines["TL1"].hyphen_pair_line_id == "TL2"
        assert lines["TL2"].hyphen_pair_line_id == "TL1"


# ===========================================================================
# Test 2: Simple PART2-only (no forward hyphen)
# ===========================================================================


class TestSimplePart2:
    def test_part2_only_detected(self, tmp_path):
        xml = _alto_xml("""\
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="mot" HPOS="100" VPOS="100" WIDTH="100" HEIGHT="50"
                    SUBS_TYPE="HypPart1" SUBS_CONTENT="moteur"/>
            <HYP CONTENT="-"/>
          </TextLine>
          <TextLine ID="TL2" HPOS="100" VPOS="180" WIDTH="2000" HEIGHT="60">
            <String ID="S3" CONTENT="eur" HPOS="100" VPOS="180" WIDTH="80" HEIGHT="50"
                    SUBS_TYPE="HypPart2" SUBS_CONTENT="moteur"/>
            <SP WIDTH="20"/>
            <String ID="S4" CONTENT="tourne." HPOS="200" VPOS="180" WIDTH="180" HEIGHT="50"/>
          </TextLine>""")
        path = _write_fixture(tmp_path, "p2.xml", xml)
        pages, _ = parse_alto_file(path, "p2.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        assert lines["TL2"].hyphen_role == HyphenRole.PART2
        assert lines["TL2"].hyphen_pair_line_id == "TL1"
        # No forward link
        assert lines["TL2"].hyphen_forward_pair_id is None


# ===========================================================================
# Test 3: Chained hyphenation — BOTH role detection
# ===========================================================================


class TestChainedDetection:
    def test_both_role_detected(self, tmp_path):
        path = _write_fixture(tmp_path, "chain.xml", CHAINED_XML)
        pages, _ = parse_alto_file(path, "chain.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        assert lines["TL1"].hyphen_role == HyphenRole.PART1
        assert lines["TL2"].hyphen_role == HyphenRole.BOTH
        assert lines["TL3"].hyphen_role == HyphenRole.PART2

    def test_both_backward_link(self, tmp_path):
        path = _write_fixture(tmp_path, "chain.xml", CHAINED_XML)
        pages, _ = parse_alto_file(path, "chain.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        tl2 = lines["TL2"]
        assert tl2.hyphen_pair_line_id == "TL1"  # backward to PART1
        assert tl2.hyphen_subs_content == "praticables."

    def test_both_forward_link(self, tmp_path):
        path = _write_fixture(tmp_path, "chain.xml", CHAINED_XML)
        pages, _ = parse_alto_file(path, "chain.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        tl2 = lines["TL2"]
        assert tl2.hyphen_forward_pair_id == "TL3"  # forward to PART2
        assert tl2.hyphen_forward_subs_content == "desservent"

    def test_part3_backward_link(self, tmp_path):
        path = _write_fixture(tmp_path, "chain.xml", CHAINED_XML)
        pages, _ = parse_alto_file(path, "chain.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        tl3 = lines["TL3"]
        assert tl3.hyphen_pair_line_id == "TL2"
        assert tl3.hyphen_subs_content == "desservent"


# ===========================================================================
# Test 4: Chained reconciliation — coherent chain
# ===========================================================================


class TestChainedReconciliation:
    def test_coherent_chain(self, tmp_path):
        """Both pairs in the chain are coherent after correction."""
        path = _write_fixture(tmp_path, "chain.xml", CHAINED_XML)
        pages, _ = parse_alto_file(path, "chain.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        # Simulate corrections that preserve hyphen structure
        lines["TL1"].corrected_text = "rendre pratica-"
        lines["TL2"].corrected_text = "bles. Les chemins des-"
        lines["TL3"].corrected_text = "servent bien."

        # Reconcile pair 1: TL1 (PART1) → TL2 (BOTH backward)
        p1, p2 = lines["TL1"], lines["TL2"]
        final_p1, final_p2, subs1 = reconcile_hyphen_pair(
            p1,
            p2,
            p1.corrected_text,
            p2.corrected_text,
        )
        p1.corrected_text = final_p1
        p1.hyphen_subs_content = subs1
        p2.corrected_text = final_p2
        p2.hyphen_subs_content = subs1
        assert subs1 == "praticables."

        # Reconcile pair 2: TL2 (BOTH forward) → TL3 (PART2)
        p2_as_p1 = copy(p2)
        p2_as_p1.hyphen_role = HyphenRole.PART1
        p2_as_p1.hyphen_subs_content = p2.hyphen_forward_subs_content
        p2_as_p1.hyphen_source_explicit = p2.hyphen_forward_explicit

        final_p2b, final_p3, subs2 = reconcile_hyphen_pair(
            p2_as_p1,
            lines["TL3"],
            p2.corrected_text,
            lines["TL3"].corrected_text,
        )
        p2.corrected_text = final_p2b
        p2.hyphen_forward_subs_content = subs2
        lines["TL3"].corrected_text = final_p3
        lines["TL3"].hyphen_subs_content = subs2
        assert subs2 == "desservent"

    def test_forward_pair_neutralised(self, tmp_path):
        """Forward pair of BOTH line fails → only forward subs neutralised."""
        path = _write_fixture(tmp_path, "chain.xml", CHAINED_XML)
        pages, _ = parse_alto_file(path, "chain.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        lines["TL1"].corrected_text = "rendre pratica-"
        lines["TL2"].corrected_text = "bles. Les chemins des-"
        lines["TL3"].corrected_text = "tructions bien."  # des+tructions ≠ desservent

        # Pair 1 coherent
        p1, p2 = lines["TL1"], lines["TL2"]
        _, _, subs1 = reconcile_hyphen_pair(p1, p2, p1.corrected_text, p2.corrected_text)
        assert subs1 == "praticables."

        # Pair 2 fallback (des+tructions ≠ desservent)
        p2_as_p1 = copy(p2)
        p2_as_p1.hyphen_role = HyphenRole.PART1
        p2_as_p1.hyphen_subs_content = p2.hyphen_forward_subs_content
        p2_as_p1.hyphen_source_explicit = p2.hyphen_forward_explicit

        final_p2b, final_p3, subs2 = reconcile_hyphen_pair(
            p2_as_p1,
            lines["TL3"],
            p2.corrected_text,
            lines["TL3"].corrected_text,
        )
        # Forward pair falls back
        assert subs2 is None
        assert final_p2b == p2.ocr_text  # reverted
        assert final_p3 == lines["TL3"].ocr_text  # reverted


# ===========================================================================
# Test 5: Chained rewriting — SUBS on both ends of BOTH line
# ===========================================================================


class TestChainedRewriting:
    def test_both_line_subs_on_both_strings(self, tmp_path):
        """A BOTH line gets backward SUBS on first String, forward SUBS on last."""
        path = _write_fixture(tmp_path, "chain.xml", CHAINED_XML)
        pages, _ = parse_alto_file(path, "chain.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        # No corrections → untouched, SUBS should be preserved from source
        xml_bytes, metrics, _paths = rewrite_alto_file(path, pages, "test", "model")
        root = etree.fromstring(xml_bytes)

        # TL2 first String (PART2 side): SUBS_TYPE=HypPart2, SUBS_CONTENT=praticables.
        st_bwd, sc_bwd = _get_subs(root, "TL2", "first")
        assert st_bwd == "HypPart2"
        assert sc_bwd == "praticables."

        # TL2 last String (PART1 side): SUBS_TYPE=HypPart1, SUBS_CONTENT=desservent
        st_fwd, sc_fwd = _get_subs(root, "TL2", "last")
        assert st_fwd == "HypPart1"
        assert sc_fwd == "desservent"


# ===========================================================================
# Test 6: Real corpus chained zone from X0000002.xml
# ===========================================================================

X0000002_PATH = Path(__file__).resolve().parent.parent.parent / "examples" / "X0000002.xml"


@pytest.mark.skipif(not X0000002_PATH.exists(), reason="X0000002.xml not found")
class TestX0000002ChainedZone:
    """Verify chained zone TL000016-TL000019 in real corpus."""

    def test_chained_zone_structure(self):
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = {lm.line_id: lm for pg in pages for lm in pg.lines}

        tl16 = lines["PAG_00000002_TL000016"]
        tl17 = lines["PAG_00000002_TL000017"]
        tl18 = lines["PAG_00000002_TL000018"]

        assert tl16.hyphen_role == HyphenRole.PART1
        assert tl17.hyphen_role == HyphenRole.BOTH
        assert tl18.hyphen_role in (HyphenRole.PART2, HyphenRole.BOTH)

        # TL17 backward: linked to TL16, subs=praticables.
        assert tl17.hyphen_pair_line_id == tl16.line_id
        assert tl17.hyphen_subs_content == "praticables."

        # TL17 forward: linked to TL18, subs=desservent
        assert tl17.hyphen_forward_pair_id == tl18.line_id
        assert tl17.hyphen_forward_subs_content == "desservent"

    def test_zero_orphan_part2(self):
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = {lm.line_id: lm for pg in pages for lm in pg.lines}

        unpaired = [
            lm
            for lm in lines.values()
            if lm.hyphen_role == HyphenRole.PART2 and not lm.hyphen_pair_line_id
        ]
        assert len(unpaired) == 0, f"Orphan PART2: {[lm.line_id for lm in unpaired]}"


# ===========================================================================
# Test 7: Diagnostic text traces
# ===========================================================================


class TestDiagnosticTraces:
    """Verify text traces through the pipeline for a chained case."""

    def test_trace_chained_correction(self, tmp_path):
        path = _write_fixture(tmp_path, "chain.xml", CHAINED_XML)
        pages, _ = parse_alto_file(path, "chain.xml")
        lines = {lm.line_id: lm for lm in pages[0].lines}

        # 1. Source OCR text
        traces: dict[str, LineTrace] = {}
        for lid, lm in lines.items():
            traces[lid] = LineTrace(
                line_id=lid,
                source_ocr_text=lm.ocr_text,
                model_corrected_text="",
                projected_text="",
                output_alto_text="",
            )

        # 2. Simulate LLM corrections
        corrections = {
            "TL1": "rendre pratica-",
            "TL2": "bles. Les chemins des-",
            "TL3": "servent bien.",
        }
        for lid, text in corrections.items():
            lines[lid].corrected_text = text
            traces[lid].model_corrected_text = text

        # 3. Reconcile pair 1
        p1, p2 = lines["TL1"], lines["TL2"]
        final_p1, final_p2, subs1 = reconcile_hyphen_pair(
            p1,
            p2,
            p1.corrected_text,
            p2.corrected_text,
        )
        p1.corrected_text = final_p1
        p1.hyphen_subs_content = subs1
        p2.corrected_text = final_p2
        p2.hyphen_subs_content = subs1

        # 3b. Reconcile pair 2
        p2_as_p1 = copy(p2)
        p2_as_p1.hyphen_role = HyphenRole.PART1
        p2_as_p1.hyphen_subs_content = p2.hyphen_forward_subs_content
        p2_as_p1.hyphen_source_explicit = p2.hyphen_forward_explicit

        final_p2b, final_p3, subs2 = reconcile_hyphen_pair(
            p2_as_p1,
            lines["TL3"],
            p2.corrected_text,
            lines["TL3"].corrected_text,
        )
        p2.corrected_text = final_p2b
        p2.hyphen_forward_subs_content = subs2
        lines["TL3"].corrected_text = final_p3
        lines["TL3"].hyphen_subs_content = subs2

        # 4. Record projected text (after reconciliation, before rewrite)
        for lid, lm in lines.items():
            traces[lid].projected_text = lm.corrected_text or lm.ocr_text

        # 5. Rewrite
        xml_bytes, metrics, _paths = rewrite_alto_file(path, pages, "test", "model")
        root = etree.fromstring(xml_bytes)

        # 6. Re-extract from output ALTO
        for lid in traces:
            traces[lid].output_alto_text = _extract_output_text(root, lid)

        # --- Verify trace consistency ---
        # TL1: source=OCR, model=corrected, projected=corrected, output=corrected
        t1 = traces["TL1"]
        assert t1.source_ocr_text == "rendre pratica-"
        assert t1.model_corrected_text == "rendre pratica-"
        assert t1.projected_text == "rendre pratica-"
        assert t1.output_alto_text == "rendre pratica-"

        # TL2 (BOTH): text preserved through chain
        t2 = traces["TL2"]
        assert t2.source_ocr_text == "bles. Les chemins des-"
        assert t2.model_corrected_text == "bles. Les chemins des-"
        assert t2.projected_text == "bles. Les chemins des-"
        assert t2.output_alto_text == "bles. Les chemins des-"

        # TL3: preserved
        t3 = traces["TL3"]
        assert t3.source_ocr_text == "servent bien."
        assert t3.projected_text == "servent bien."
        assert t3.output_alto_text == "servent bien."

        # Print traces for diagnostic visibility
        print("\n=== DIAGNOSTIC TRACES (chained) ===")
        for lid in ("TL1", "TL2", "TL3"):
            t = traces[lid]
            print(f"  {lid}:")
            print(f"    source_ocr:     {t.source_ocr_text!r}")
            print(f"    model_corrected:{t.model_corrected_text!r}")
            print(f"    projected:      {t.projected_text!r}")
            print(f"    output_alto:    {t.output_alto_text!r}")
