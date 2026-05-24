"""Smoke tests for the alto-core package.

These tests don't try to be exhaustive — the heavy lifting is still
done by the backend test suite that exercises the modules through the
re-export shim. The goal here is to catch the most obvious extraction
mistakes: missing files, broken imports, exported symbols that vanished.

When the alto-core package gets its own consumer (eScriptorium bridge,
benchmark runner, etc.), this file will grow into a full test surface.
"""

from __future__ import annotations


def test_top_level_import():
    import alto_core

    assert isinstance(alto_core.__version__, str)
    assert alto_core.__version__.startswith("0.")


def test_subpackages_importable():
    import alto_core.alto.hyphenation
    import alto_core.alto.parser
    import alto_core.alto.rewriter
    import alto_core.pipeline.chunk_planner
    import alto_core.pipeline.correction_pipeline
    import alto_core.pipeline.line_acceptance
    import alto_core.pipeline.validator
    import alto_core.protocols
    import alto_core.protocols.provider
    import alto_core.schemas

    # Touch attributes that consumers will reach for, so a missing
    # rename in the extraction trips here rather than at first call.
    assert alto_core.pipeline.correction_pipeline.CorrectionPipeline
    assert alto_core.protocols.BaseProvider
    assert alto_core.protocols.PipelineObserver
    assert alto_core.protocols.OutputWriter
    assert alto_core.protocols.provider.OUTPUT_JSON_SCHEMA
    assert alto_core.protocols.provider.SYSTEM_PROMPT
    assert alto_core.schemas.LineManifest
    assert alto_core.schemas.DocumentManifest


def test_correction_pipeline_construction_does_not_touch_infrastructure():
    """A bare ``CorrectionPipeline`` should instantiate from mock ports —
    no filesystem, no HTTP, no global state."""
    from alto_core.pipeline.correction_pipeline import CorrectionPipeline

    class _NoopProvider:
        async def list_models(self, api_key):  # pragma: no cover
            return []

        async def complete_structured(self, **_kwargs):  # pragma: no cover
            return {"lines": []}

    class _NoopObserver:
        def on_event(self, event_type, payload):
            pass

    class _NoopWriter:
        def write_corrected(self, *, source_stem, xml_bytes):
            pass

        def write_trace(self, *, traces_payload):
            pass

    pipeline = CorrectionPipeline(
        provider=_NoopProvider(),
        observer=_NoopObserver(),
        output_writer=_NoopWriter(),
    )
    assert pipeline.provider is not None
    assert pipeline.observer is not None
    assert pipeline.output_writer is not None
