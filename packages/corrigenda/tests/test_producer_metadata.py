"""P3.7-4 — ProducerMetadata replaces the bare provider_name/model pair.

The provenance identity of a run is a structured envelope on the
producer seam: producers DECLARE it (optional ``metadata`` attribute,
same convention as ``requires_full_coverage``), the constructor's
explicit ``producer_metadata`` overrides it, and ``for_provider`` keeps
its pinned vendor vocabulary by building the envelope from
``provider_name``/``model``. The §11 labels stamped into corrected XML
derive from the envelope via ``provenance_labels()``.
"""

from __future__ import annotations

from corrigenda.core.pipeline import CorrectionPipeline
from corrigenda.core.protocols import ProducerMetadata
from corrigenda.producers.llm_edit import LLMEditProducer
from corrigenda.producers.rules import RulesProducer, default_french_ocr_rules

from tests._pipeline_harness import DictProvider


class _Null:
    def on_event(self, event_type, payload):
        pass


class _BareProducer:
    """A producer declaring nothing — no metadata attribute at all."""

    wants_geometry = False
    wants_image = False

    async def produce(self, payload, *, options):  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# The envelope itself
# ---------------------------------------------------------------------------


def test_defaults_reproduce_the_historical_unknown_labels():
    md = ProducerMetadata()
    assert md.name == "unknown"
    assert md.version is None
    assert md.implementation is None
    assert md.configuration_fingerprint is None
    assert md.provenance_labels() == ("unknown", "unknown")


def test_provenance_labels_map_name_and_implementation():
    md = ProducerMetadata(name="openai", implementation="gpt-x")
    assert md.provenance_labels() == ("openai", "gpt-x")
    # An implementation-less producer stamps "unknown" for the model
    # slot — byte-compatible with the bare-string era.
    assert ProducerMetadata(name="rules").provenance_labels() == (
        "rules",
        "unknown",
    )


# ---------------------------------------------------------------------------
# Resolution order on the constructor
# ---------------------------------------------------------------------------


def test_pipeline_without_any_metadata_is_anonymous():
    pipeline = CorrectionPipeline(producer=_BareProducer(), observer=_Null())
    assert pipeline.producer_metadata == ProducerMetadata()


def test_pipeline_reads_the_producer_declaration():
    producer = RulesProducer(default_french_ocr_rules())
    pipeline = CorrectionPipeline(producer=producer, observer=_Null())
    assert pipeline.producer_metadata is producer.metadata
    assert pipeline.producer_metadata.name == "rules"


def test_explicit_constructor_metadata_wins_over_the_declaration():
    producer = RulesProducer(default_french_ocr_rules())
    explicit = ProducerMetadata(name="my-rules", implementation="fr-v2")
    pipeline = CorrectionPipeline(
        producer=producer, observer=_Null(), producer_metadata=explicit
    )
    assert pipeline.producer_metadata is explicit


def test_non_metadata_attribute_is_ignored():
    class _Odd(_BareProducer):
        metadata = {"name": "not-the-right-type"}

    pipeline = CorrectionPipeline(producer=_Odd(), observer=_Null())
    assert pipeline.producer_metadata == ProducerMetadata()


# ---------------------------------------------------------------------------
# for_provider keeps the vendor vocabulary and builds the envelope
# ---------------------------------------------------------------------------


def test_for_provider_maps_vendor_strings_into_the_envelope():
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}),
        api_key="k",
        model="gpt-x",
        provider_name="openai",
        observer=_Null(),
    )
    assert pipeline.producer_metadata.name == "openai"
    assert pipeline.producer_metadata.implementation == "gpt-x"


def test_for_provider_carries_the_producer_configuration_fingerprint():
    """The explicit vendor envelope overrides IDENTITY (name/model); it
    must not erase the producer's configuration provenance (§11)."""
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}),
        api_key="k",
        model="gpt-x",
        provider_name="openai",
        observer=_Null(),
    )
    direct = LLMEditProducer(DictProvider({}), "k", "gpt-x")
    assert (
        pipeline.producer_metadata.configuration_fingerprint
        == direct.metadata.configuration_fingerprint
    )
    assert pipeline.producer_metadata.configuration_fingerprint is not None


def test_for_provider_default_name_stays_unknown():
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}), api_key="k", model="m", observer=_Null()
    )
    # The explicit envelope (name="unknown") wins over LLMEditProducer's
    # own generic declaration — the XML stamp is unchanged from the
    # bare-string era for callers who never passed provider_name.
    assert pipeline.producer_metadata.provenance_labels() == ("unknown", "m")


# ---------------------------------------------------------------------------
# Producer declarations
# ---------------------------------------------------------------------------


def test_llm_edit_producer_declares_generic_llm_identity():
    producer = LLMEditProducer(DictProvider({}), "k", "gpt-x")
    assert producer.metadata.name == "llm"
    assert producer.metadata.implementation == "gpt-x"


def test_llm_edit_producer_declares_configuration_fingerprint():
    """Phase 0 (ROADMAP V3) — an LLM run's provenance must say WHICH
    prompt/schema contract produced the edits, not just which model.
    Failed before: metadata carried no configuration_fingerprint."""
    md = LLMEditProducer(DictProvider({}), "k", "gpt-x").metadata
    assert md.configuration_fingerprint is not None
    assert len(md.configuration_fingerprint) == 16


def test_llm_fingerprint_is_deterministic_and_contract_sensitive():
    a = LLMEditProducer(DictProvider({}), "k", "gpt-x").metadata
    b = LLMEditProducer(DictProvider({}), "other-key", "gpt-y").metadata
    # Credentials and model are NOT configuration: same contract, same digest
    # (the model already lives in `implementation`).
    assert a.configuration_fingerprint == b.configuration_fingerprint

    with_prompt = LLMEditProducer(
        DictProvider({}), "k", "gpt-x", system_prompt="autre prompt"
    ).metadata
    assert with_prompt.configuration_fingerprint != a.configuration_fingerprint

    with_schema = LLMEditProducer(
        DictProvider({}), "k", "gpt-x", output_schema={"type": "object"}
    ).metadata
    assert with_schema.configuration_fingerprint != a.configuration_fingerprint


def test_rules_producer_declares_configuration_fingerprint():
    producer = RulesProducer(default_french_ocr_rules())
    md = producer.metadata
    assert md.name == "rules"
    assert md.implementation is None  # a rules engine has no "model"
    assert md.configuration_fingerprint is not None
    assert len(md.configuration_fingerprint) == 16


def test_rules_fingerprint_is_deterministic_and_config_sensitive():
    a = RulesProducer(default_french_ocr_rules()).metadata
    b = RulesProducer(default_french_ocr_rules()).metadata
    assert a.configuration_fingerprint == b.configuration_fingerprint

    with_lexicon = RulesProducer(
        default_french_ocr_rules(), lexicon={"moderne"}
    ).metadata
    assert with_lexicon.configuration_fingerprint != a.configuration_fingerprint, (
        "lexicon must be part of the producer configuration digest"
    )
