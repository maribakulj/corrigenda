"""P3.9 (§11) — RunProvenance: the report says exactly WHAT produced it.

Beyond the policy fingerprint: library version, generic producer
identity, per-file sha256 of the INPUT bytes, source format, and
critical dependency versions — present on every run, dry runs included.
(The XML-side processingStep stamp is covered by ``test_provenance.py``.)
"""

from __future__ import annotations

import hashlib
from dataclasses import fields as dataclass_fields
from pathlib import Path

from corrigenda import CorrectionPipeline, __version__
from corrigenda.core.protocols import ProducerMetadata
from corrigenda.core.schemas import ProducerProvenance, RunProvenance
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.producers.rules import RulesProducer, default_french_ocr_rules

from tests._pipeline_harness import EXAMPLES, DictProvider, RecordingObserver

_SAMPLE = EXAMPLES / "sample.xml"


def _run(**pipeline_kwargs):
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}),
        api_key="k",
        model="m",
        provider_name="test-prov",
        observer=RecordingObserver(),
        **pipeline_kwargs,
    )
    return pipeline.run_sync(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )


def test_report_carries_full_provenance():
    result = _run()
    prov = result.report.provenance
    assert prov is not None
    assert prov.lib_version == __version__
    # Same composite the XML processingStep stamps.
    assert len(prov.config_fingerprint) == 16
    assert prov.producer.name == "test-prov"
    assert prov.producer.implementation == "m"
    assert prov.source_format == "alto"
    # Both critical dependencies are installed in the test environment.
    assert set(prov.dependencies) == {"lxml", "pydantic"}
    assert all(v for v in prov.dependencies.values())


def test_source_digest_matches_the_input_bytes():
    result = _run()
    prov = result.report.provenance
    assert prov is not None
    expected = "sha256:" + hashlib.sha256(_SAMPLE.read_bytes()).hexdigest()
    assert prov.source_digests == {_SAMPLE.name: expected}


def test_dry_run_still_carries_provenance():
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}), api_key="k", model="m", observer=RecordingObserver()
    )
    result = pipeline.run_sync(document_manifest=doc, source_files={})
    prov = result.report.provenance
    assert prov is not None
    assert prov.source_digests == {}  # nothing was given, nothing digested
    assert prov.lib_version == __version__


def test_rules_producer_provenance_has_no_artificial_model():
    """Generic vocabulary end to end: a rules run's report says WHO
    (name) + its configuration digest — never a fabricated model."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    producer = RulesProducer(default_french_ocr_rules())

    class _Null:
        def on_event(self, event_type, payload):
            pass

    pipeline = CorrectionPipeline(producer=producer, observer=_Null())
    result = pipeline.run_sync(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )
    prov = result.report.provenance
    assert prov is not None
    assert prov.producer.name == "rules"
    assert prov.producer.implementation is None
    assert prov.producer.configuration_fingerprint is not None


def test_producer_provenance_mirrors_producer_metadata_fields():
    """ProducerProvenance is the report-side mirror of the protocols
    dataclass — a field drifting on one side must trip here."""
    metadata_fields = {f.name for f in dataclass_fields(ProducerMetadata)}
    assert set(ProducerProvenance.model_fields) == metadata_fields


def test_provenance_round_trips_through_report_json():
    result = _run()
    payload = result.report.model_dump_json()
    from corrigenda.core.schemas import CorrectionReport

    restored = CorrectionReport.model_validate_json(payload)
    assert restored.provenance == result.report.provenance
    assert isinstance(restored.provenance, RunProvenance)


def test_write_persists_provenance_in_report_json(tmp_path: Path):
    result = _run()
    result.write(tmp_path)
    text = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert '"provenance"' in text
    assert '"source_digests"' in text
