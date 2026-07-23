"""ROADMAP V3 Phase 1 — LossPolicy token_realign gate + sidecar.

The middle ground between REPORT (project everything, count losses) and
STRICT (reject every word-count change): a correction projects only
when its token alignment onto the source is confident; a gated line
reverts to source markup, and its correction is PRESERVED in the
sidecar (report.sidecar / sidecar.json) instead of lost.
"""

from __future__ import annotations

import json
from pathlib import Path

from corrigenda import CorrectionPipeline, LossPolicy
from corrigenda.formats.alto.parser import build_document_manifest

from tests._pipeline_harness import DictProvider

_NS = "http://www.loc.gov/standards/alto/ns-v3#"

_ALTO = f"""<?xml version="1.0"?>
<alto xmlns="{_NS}">
  <Layout>
    <Page ID="P1" WIDTH="600" HEIGHT="800">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="600" HEIGHT="800">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="600" HEIGHT="90">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="300" HEIGHT="30">
            <String ID="S1" CONTENT="la" HPOS="0" VPOS="0" WIDTH="60" HEIGHT="30"/>
            <SP HPOS="60" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="S2" CONTENT="rnaison" HPOS="70" VPOS="0" WIDTH="110" HEIGHT="30"/>
            <SP HPOS="180" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="S3" CONTENT="blanche" HPOS="190" VPOS="0" WIDTH="110" HEIGHT="30"/>
          </TextLine>
          <TextLine ID="L2" HPOS="0" VPOS="30" WIDTH="300" HEIGHT="30">
            <String ID="S4" CONTENT="grande" HPOS="0" VPOS="30" WIDTH="140" HEIGHT="30"/>
            <SP HPOS="140" VPOS="30" WIDTH="10" HEIGHT="30"/>
            <String ID="S5" CONTENT="porte" HPOS="150" VPOS="30" WIDTH="150" HEIGHT="30"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


class _Null:
    def on_event(self, *a, **k):
        pass


def _run(tmp_path: Path, corrections: dict[str, str], policy: LossPolicy | None):
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider(corrections),
        api_key="k",
        model="m",
        observer=_Null(),
        loss_policy=policy,
    )
    return pipeline.run_sync(
        document_manifest=doc, source_files={xml_path.name: xml_path}
    )


def test_default_policy_keeps_historical_behaviour(tmp_path: Path):
    """Gate off: a guard-passing expansion with a weak token alignment
    (0.45 — plausible LLM filler the source does not support) still
    projects (REPORT stance) and no sidecar exists."""
    result = _run(tmp_path, {"L1": "la maison de la ville blanche"}, None)
    assert result.report.sidecar is None
    assert b'CONTENT="ville"' in result.corrected_files["p.xml"]


def test_confident_realignment_projects(tmp_path: Path):
    """An ordinary correction + insertion aligns far above the
    threshold: it projects, nothing goes to the sidecar."""
    policy = LossPolicy(min_alignment_score=0.6)
    result = _run(tmp_path, {"L1": "la maison très blanche"}, policy)
    assert result.report.sidecar is None
    out = result.corrected_files["p.xml"]
    assert b'CONTENT="maison"' in out
    assert b'CONTENT="tr\xc3\xa8s"' in out


def test_low_alignment_correction_goes_to_sidecar(tmp_path: Path):
    """Failed before the gate existed: a guard-PASSING expansion whose
    tokens barely correspond to the source (score 0.45) projected
    anyway. Now the XML keeps the source markup and the correction is
    preserved for review."""
    policy = LossPolicy(min_alignment_score=0.6)
    result = _run(tmp_path, {"L1": "la maison de la ville blanche"}, policy)

    out = result.corrected_files["p.xml"]
    assert b'CONTENT="ville"' not in out
    assert b'CONTENT="rnaison"' in out  # source markup intact

    assert result.report.sidecar is not None
    [entry] = result.report.sidecar
    assert (entry.page_id, entry.line_id) == ("P1", "L1")
    assert entry.corrected_text == "la maison de la ville blanche"
    assert entry.source_text == "la rnaison blanche"
    assert entry.alignment_score is not None and entry.alignment_score < 0.6
    assert not entry.move_suspected
    assert "token_realign" in entry.reason

    # The line's decision is an honest fallback, not a silent no-op.
    from corrigenda import LineRef

    decision = result.decisions.by_ref[LineRef(page_id="P1", line_id="L1")]
    assert decision.status.value == "fallback"
    assert decision.final_text == "la rnaison blanche"


def test_same_count_reorder_is_gated_by_the_move_flag(tmp_path: Path):
    """A same-word-count swap would project through the FAST path and
    glue identities to swapped words — the move flag gates it too."""
    policy = LossPolicy(min_alignment_score=0.6)
    result = _run(tmp_path, {"L2": "porte grande"}, policy)

    out = result.corrected_files["p.xml"]
    assert b'CONTENT="grande" HPOS="0"' in out  # source order intact
    assert result.report.sidecar is not None
    [entry] = result.report.sidecar
    assert entry.line_id == "L2"
    assert entry.move_suspected


def test_write_emits_sidecar_json_only_when_needed(tmp_path: Path):
    policy = LossPolicy(min_alignment_score=0.6)
    result = _run(tmp_path, {"L1": "la maison de la ville blanche"}, policy)
    written = result.write(tmp_path / "out")
    sidecar_path = tmp_path / "out" / "sidecar.json"
    assert sidecar_path in written
    entries = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert entries[0]["line_id"] == "L1"

    clean = _run(tmp_path, {"L1": "la maison blanche"}, policy)
    written = clean.write(tmp_path / "out2")
    assert not (tmp_path / "out2" / "sidecar.json").exists()
    assert all(p.name != "sidecar.json" for p in written)


def test_gate_changes_the_policy_fingerprint(tmp_path: Path):
    """§11 — a run under the gate must not stamp the default policy's
    fingerprint."""
    assert (
        LossPolicy(min_alignment_score=0.6).policy_fingerprint()
        != LossPolicy().policy_fingerprint()
    )
