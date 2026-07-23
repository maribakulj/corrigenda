"""ROADMAP V3 Phase 1 — the LLM uncertainty channel.

Doctrine under test: the model supplies AUDITABLE EVIDENCE (a status and
reason-coded per-token claims), never a raw score; the app verifies
every verifiable claim (confusion table, lexicon), a failed check
scores below an honest admission of guessing, and the verified value
feeds the ``producer`` component of ``LineConfidence``. Off by default:
the base prompt/schema stay byte-identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corrigenda import ConfidencePolicy, CorrectionPipeline, Usage
from corrigenda.core.confidence import score_producer_claims
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.integrations.llm import (
    OUTPUT_JSON_SCHEMA,
    SYSTEM_PROMPT,
    UNCERTAINTY_REASONS,
    uncertainty_output_schema,
    uncertainty_system_prompt,
)
from corrigenda.producers.llm_edit import LLMEditProducer

from tests._pipeline_harness import DictProvider


class _Null:
    def on_event(self, *a, **k):
        pass


# ---------------------------------------------------------------------------
# Claim verification (pure)
# ---------------------------------------------------------------------------


def test_no_declaration_scores_none():
    assert (
        score_producer_claims(
            source_text="la rnaison",
            corrected_text="la maison",
            status=None,
            claims=[],
        )
        is None
    )


def test_uncertain_status_caps_low_whatever_the_claims():
    score = score_producer_claims(
        source_text="la rnaison",
        corrected_text="la maison",
        status="uncertain",
        claims=[{"source": "rnaison", "corrected": "maison", "reason": "confusion_connue"}],
    )
    assert score == pytest.approx(0.3)


def test_verified_confusion_claim_scores_high():
    score = score_producer_claims(
        source_text="la rnaison",
        corrected_text="la maison",
        status="certain",
        claims=[{"source": "rnaison", "corrected": "maison", "reason": "confusion_connue"}],
    )
    assert score == pytest.approx(0.95)


def test_false_confusion_claim_scores_below_an_honest_guess():
    """A fabricated justification is WORSE evidence than admitting a
    conjecture."""
    lied = score_producer_claims(
        source_text="la bleue",
        corrected_text="la rouge",
        status="certain",
        claims=[{"source": "bleue", "corrected": "rouge", "reason": "confusion_connue"}],
    )
    honest = score_producer_claims(
        source_text="la bleue",
        corrected_text="la rouge",
        status="certain",
        claims=[{"source": "bleue", "corrected": "rouge", "reason": "conjecture"}],
    )
    assert lied == pytest.approx(0.2)
    assert honest == pytest.approx(0.3)
    assert lied < honest


def test_lexicon_claim_is_verified_against_the_lexicon():
    kwargs: dict[str, Any] = dict(
        source_text="la vieifle",
        corrected_text="la vieille",
        status="certain",
        claims=[{"source": "vieifle", "corrected": "vieille", "reason": "mot_du_lexique"}],
    )
    assert score_producer_claims(**kwargs, lexicon={"vieille"}) == pytest.approx(0.9)
    # No lexicon configured → the claim is unverifiable → failed.
    assert score_producer_claims(**kwargs) == pytest.approx(0.2)


def test_claim_with_fabricated_tokens_fails():
    score = score_producer_claims(
        source_text="la rnaison",
        corrected_text="la maison",
        status="certain",
        claims=[{"source": "jamais", "corrected": "présent", "reason": "confusion_connue"}],
    )
    assert score == pytest.approx(0.2)


def test_line_score_is_the_weakest_claim():
    score = score_producer_claims(
        source_text="la rnaison tronblée",
        corrected_text="la maison troublée",
        status="certain",
        claims=[
            {"source": "rnaison", "corrected": "maison", "reason": "confusion_connue"},
            {"source": "tronblée", "corrected": "troublée", "reason": "conjecture"},
        ],
    )
    assert score == pytest.approx(0.3)


def test_bare_certain_is_the_classic_miscalibrated_signal():
    score = score_producer_claims(
        source_text="la rnaison",
        corrected_text="la maison",
        status="certain",
        claims=[],
    )
    assert score == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Contract variant (prompt + schema) and the producer
# ---------------------------------------------------------------------------


def test_channel_off_keeps_the_base_contract_byte_identical():
    producer = LLMEditProducer(DictProvider({}), "k", "m")
    assert producer._system_prompt == SYSTEM_PROMPT
    assert producer._output_schema == OUTPUT_JSON_SCHEMA


def test_channel_on_extends_prompt_schema_and_fingerprint():
    base = LLMEditProducer(DictProvider({}), "k", "m")
    channel = LLMEditProducer(DictProvider({}), "k", "m", uncertainty_channel=True)
    assert channel._system_prompt == uncertainty_system_prompt()
    assert "status" in channel._system_prompt
    schema = channel._output_schema
    line_props = schema["schema"]["properties"]["lines"]["items"]["properties"]
    assert line_props["status"]["enum"] == ["certain", "uncertain"]
    assert line_props["edits"]["items"]["properties"]["reason"]["enum"] == list(
        UNCERTAINTY_REASONS
    )
    # Strict structured-output modes need every property required.
    assert set(schema["schema"]["properties"]["lines"]["items"]["required"]) == {
        "line_id",
        "corrected_text",
        "status",
        "edits",
    }
    # A different contract is a different configuration fingerprint (§11).
    assert (
        channel.metadata.configuration_fingerprint
        != base.metadata.configuration_fingerprint
    )


class _ClaimingProvider:
    """Returns the uncertainty-channel shape with a verifiable claim on
    L1 and an admitted uncertainty on L2."""

    async def list_models(self, api_key: str) -> list[Any]:  # pragma: no cover
        return []

    async def complete_structured(self, **kw: Any) -> tuple[dict[str, Any], Usage | None]:
        out = []
        for ln in kw["user_payload"].get("lines", []):
            if ln["line_id"] == "L1":
                out.append(
                    {
                        "line_id": "L1",
                        "corrected_text": ln["ocr_text"].replace("rnaison", "maison"),
                        "status": "certain",
                        "edits": [
                            {
                                "source": "rnaison",
                                "corrected": "maison",
                                "reason": "confusion_connue",
                            }
                        ],
                    }
                )
            else:
                out.append(
                    {
                        "line_id": ln["line_id"],
                        "corrected_text": ln["ocr_text"],
                        "status": "uncertain",
                        "edits": [],
                    }
                )
        return {"lines": out}, None


_NS = "http://www.loc.gov/standards/alto/ns-v3#"
_ALTO = f"""<?xml version="1.0"?>
<alto xmlns="{_NS}">
  <Layout>
    <Page ID="P1" WIDTH="600" HEIGHT="800">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="600" HEIGHT="800">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="600" HEIGHT="60">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="300" HEIGHT="30">
            <String ID="S1" CONTENT="la" HPOS="0" VPOS="0" WIDTH="140" HEIGHT="30"/>
            <SP HPOS="140" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="S2" CONTENT="rnaison" HPOS="150" VPOS="0" WIDTH="150" HEIGHT="30"/>
          </TextLine>
          <TextLine ID="L2" HPOS="0" VPOS="30" WIDTH="300" HEIGHT="30">
            <String ID="S3" CONTENT="illisible" HPOS="0" VPOS="30" WIDTH="300" HEIGHT="30"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def test_verified_claims_feed_the_producer_component(tmp_path: Path):
    """End to end: channel on + report_only → LineOutcome.confidence
    carries the VERIFIED producer score, and the admitted uncertainty
    caps its line's decision."""
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    pipeline = CorrectionPipeline.for_provider(
        _ClaimingProvider(),
        api_key="k",
        model="m",
        observer=_Null(),
        uncertainty_channel=True,
        confidence_policy=ConfidencePolicy(mode="report_only"),
    )
    result = pipeline.run_sync(
        document_manifest=doc, source_files={xml_path.name: xml_path}
    )
    by_id = {o.line_id: o for o in result.report.lines}

    l1 = by_id["L1"].confidence
    assert l1 is not None
    assert l1.producer == pytest.approx(0.95)
    assert by_id["L1"].decision.final_text == "la maison"

    # L2: unchanged text, but the model ADMITTED doubt about it — the
    # producer component (0.3) is the weakest evidence and rules the
    # aggregate: exactly the review-queue signal we want.
    l2 = by_id["L2"].confidence
    assert l2 is not None
    assert l2.producer == pytest.approx(0.3)
    assert l2.alignment == 1.0
    assert l2.decision == pytest.approx(0.3)


def test_channel_off_declares_nothing(tmp_path: Path):
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}),
        api_key="k",
        model="m",
        observer=_Null(),
        confidence_policy=ConfidencePolicy(mode="report_only"),
    )
    result = pipeline.run_sync(
        document_manifest=doc, source_files={xml_path.name: xml_path}
    )
    assert all(o.confidence.producer is None for o in result.report.lines)
