"""Smoke tests for the corrigenda package.

These tests don't try to be exhaustive — the heavy lifting is still
done by the backend test suite that exercises the modules through the
re-export shim. The goal here is to catch the most obvious extraction
mistakes: missing files, broken imports, exported symbols that vanished.

When the corrigenda package gets its own consumer (eScriptorium bridge,
benchmark runner, etc.), this file will grow into a full test surface.
"""

from __future__ import annotations


def test_top_level_import():
    import corrigenda

    assert isinstance(corrigenda.__version__, str)
    # X.Y.Z semver shape — the exact value is the release's business, but a
    # malformed version string breaks packaging (hatchling reads this).
    parts = corrigenda.__version__.split(".")
    assert len(parts) >= 3 and parts[0].isdigit(), corrigenda.__version__


def test_subpackages_importable():
    import corrigenda.core.hyphenation
    import corrigenda.formats.alto.parser
    import corrigenda.formats.alto.rewriter
    import corrigenda.core.planner
    import corrigenda.core.pipeline
    import corrigenda.core.guards
    import corrigenda.core.validator
    import corrigenda.core.protocols
    import corrigenda.producers.llm
    import corrigenda.core.schemas

    # Touch attributes that consumers will reach for, so a missing
    # rename in the extraction trips here rather than at first call.
    assert corrigenda.core.pipeline.CorrectionPipeline
    assert corrigenda.core.protocols.BaseProvider
    assert corrigenda.core.protocols.PipelineObserver
    assert corrigenda.core.protocols.OutputWriter
    assert corrigenda.core.protocols.FormatAdapter
    assert corrigenda.producers.llm.OUTPUT_JSON_SCHEMA
    assert corrigenda.producers.llm.SYSTEM_PROMPT
    assert corrigenda.core.schemas.LineManifest
    assert corrigenda.core.schemas.DocumentManifest


def test_top_level_public_api_is_importable():
    """The README and ARCHITECTURE.md promise a single import surface.
    If a future refactor drops one of these re-exports, this test trips.

    The list MUST stay in sync with ``corrigenda.__all__`` (less
    ``__version__`` which is checked separately in
    ``test_top_level_import``). The shared smoke script
    ``packages/corrigenda/_smoke_imports.py`` iterates ``__all__``
    directly to enforce the same contract from CI/release tooling.
    """
    from corrigenda import (
        OUTPUT_JSON_SCHEMA,
        SYSTEM_PROMPT,
        BaseProvider,
        BlockManifest,
        ChunkGranularity,
        ChunkPlannerConfig,
        CorrectionPipeline,
        CorrectionResult,
        DocumentManifest,
        HyphenRole,
        LineManifest,
        LineStatus,
        LineTrace,
        LLMLineInput,
        LLMLineOutput,
        ModelInfo,
        OutputWriter,
        PageManifest,
        PipelineObserver,
        build_document_manifest,
        extract_output_texts,
        parse_alto_file,
        rewrite_alto_file,
        sanitize_error,
    )

    # Just touch each one so flake/mypy can't optimise the import away.
    assert all(
        x is not None
        for x in (
            BaseProvider,
            PipelineObserver,
            OutputWriter,
            CorrectionPipeline,
            CorrectionResult,
            build_document_manifest,
            parse_alto_file,
            rewrite_alto_file,
            extract_output_texts,
            OUTPUT_JSON_SCHEMA,
            SYSTEM_PROMPT,
            sanitize_error,
            DocumentManifest,
            PageManifest,
            BlockManifest,
            LineManifest,
            HyphenRole,
            LineStatus,
            ChunkGranularity,
            ChunkPlannerConfig,
            ModelInfo,
            LineTrace,
            LLMLineInput,
            LLMLineOutput,
        )
    )


def test_all_matches_top_level_attrs():
    """Roadmap L5 (P8) — ``corrigenda.__all__`` must reflect what's
    actually accessible on the package object. A symbol listed in
    ``__all__`` but missing from the module would silently break
    ``from corrigenda import *`` downstream.
    """
    import corrigenda

    for name in corrigenda.__all__:
        assert hasattr(corrigenda, name), (
            f"{name!r} is listed in corrigenda.__all__ but not present "
            f"on the corrigenda module — broken __init__.py re-export"
        )


def test_changelog_added_symbols_are_importable():
    """Roadmap L5 (B5) — every symbol the CHANGELOG promises in its
    ``### Added`` section must be importable from the documented path.

    The CHANGELOG groups symbols under sub-module headings like
    ``corrigenda.formats.alto`` / ``corrigenda.core``; this test pins the
    promise so a future rename or move breaks the test before it
    breaks a PyPI consumer. The map below is the canonical list — when
    you change the CHANGELOG, sync this map (one line per move).

    NB this test does NOT assert that every listed symbol is a
    top-level re-export. The roadmap explicitly clarifies in the
    CHANGELOG that some symbols are sub-module only; that's checked
    by ``test_top_level_public_api_is_importable`` for the top-level
    set, and HERE for the broader sub-module set.
    """
    import importlib

    # (module path, [symbols expected on that module]).
    # Source of truth: packages/corrigenda/CHANGELOG.md ### Added section.
    expected: list[tuple[str, list[str]]] = [
        # corrigenda.formats.alto
        (
            "corrigenda.formats.alto.parser",
            ["parse_alto_file", "build_document_manifest"],
        ),
        (
            "corrigenda.formats.alto.rewriter",
            ["rewrite_alto_file", "extract_output_texts", "RewriterMetrics"],
        ),
        (
            "corrigenda.core.hyphenation",
            [
                "enrich_chunk_lines",
                "reconcile_hyphen_pair",
                "ReconcileMetrics",
                "classify_reconcile_outcome",
                "should_stay_in_same_chunk",
            ],
        ),
        # corrigenda.core
        (
            "corrigenda.core.pipeline",
            ["CorrectionPipeline", "CorrectionResult", "sanitize_error"],
        ),
        ("corrigenda.core.planner", ["plan_page", "downgrade_granularity"]),
        ("corrigenda.core.validator", ["validate_llm_response"]),
        (
            "corrigenda.core.guards",
            ["check_line", "check_adjacent_duplicates", "AcceptanceResult"],
        ),
        # corrigenda.core.protocols
        (
            "corrigenda.core.protocols",
            [
                "BaseProvider",
                "PipelineObserver",
                "OutputWriter",
                # P0-1 provider taxonomy (Unreleased ### Added)
                "ProviderTransientError",
                "ProviderPermanentError",
            ],
        ),
        ("corrigenda.producers.llm", ["OUTPUT_JSON_SCHEMA", "SYSTEM_PROMPT"]),
        # corrigenda.errors — P0-5 (Unreleased ### Added)
        ("corrigenda.errors", ["DuplicateIdError"]),
    ]

    missing: list[str] = []
    for module_path, symbols in expected:
        mod = importlib.import_module(module_path)
        for name in symbols:
            if not hasattr(mod, name):
                missing.append(f"{module_path}.{name}")

    assert not missing, (
        "CHANGELOG.md promises these symbols but they are not importable "
        f"from their documented path: {missing}. Either fix the import "
        f"path, fix the CHANGELOG, or update the expected map in this test."
    )


def test_correction_pipeline_construction_does_not_touch_infrastructure():
    """A bare ``CorrectionPipeline`` should instantiate from mock ports —
    no filesystem, no HTTP, no global state."""
    from corrigenda.core.pipeline import CorrectionPipeline

    class _NoopProvider:
        async def list_models(self, api_key):  # pragma: no cover
            return []

        async def complete_structured(self, **_kwargs):  # pragma: no cover
            return {"lines": []}, None

    class _NoopObserver:
        def on_event(self, event_type, payload):
            pass

    class _NoopWriter:
        def write_corrected(self, *, source_stem, xml_bytes):
            pass

        def write_trace(self, *, traces_payload):
            pass

    pipeline = CorrectionPipeline.for_provider(
        _NoopProvider(),
        api_key="k",
        model="m",
        observer=_NoopObserver(),
        output_writer=_NoopWriter(),
    )
    assert pipeline.producer is not None
    assert pipeline.observer is not None
    assert pipeline.output_writer is not None
