"""Public-API snapshot (P5) — the 1.0 surface is a contract, not an accident.

Three pins:

  1. ``corrigenda.__all__`` is EXACTLY the frozen list below. Adding a
     symbol is a deliberate act (update the snapshot + CHANGELOG); removing
     or renaming one is a breaking change (major version).
  2. Every listed symbol actually resolves — eager or lazy (PEP 562) — and
     every lazy-map key is part of ``__all__``.
  3. The signatures of the top entry points (``run``, ``run_sync``,
     ``for_provider``) and the ``CorrectionReport`` JSON keys are pinned:
     these are what consumer code and persisted artefacts depend on.

If this test fails after an intentional change, update the snapshot in the
same commit that documents the change in CHANGELOG.md.
"""

from __future__ import annotations

import inspect

import corrigenda

# ---------------------------------------------------------------------------
# 1. The frozen 1.0 surface
# ---------------------------------------------------------------------------

PUBLIC_API_1_0 = sorted(
    [
        # Version
        "__version__",
        # Parsers / rewriters / adapters (lazy — formats)
        "build_document_manifest",
        "parse_alto_file",
        "extract_output_texts",
        "rewrite_alto_file",
        "parse_page_file",
        "rewrite_page_file",
        "AltoFormatAdapter",
        "PageFormatAdapter",
        # Happy path (§2, P3.12 — lazy: formats)
        "load",
        "correct",
        "correct_sync",
        "LoadedDocument",
        # Pipeline
        "CorrectionPipeline",
        "CorrectionResult",
        # Decisions (ADR-011, slice E)
        "DecisionSet",
        "LineDecision",
        "LineRef",
        # Edit protocol (§4)
        "EDIT_PROTOCOL_VERSION",
        "EditScript",
        "EditOp",
        "ReplaceLine",
        "ReplaceSpan",
        "MatchAnchor",
        "RangeAnchor",
        "EditResult",
        "EditRejection",
        "LinePrecondition",
        "apply_edit_script",
        "line_digest",
        "normalize_anchor",
        # Producers (§5)
        "EditProducer",
        "ProducerMetadata",
        "ProducerOptions",
        "require_page_images",
        "RulesProducer",
        "SubstitutionRule",
        "default_french_ocr_rules",
        "LLMEditProducer",
        # Errors (§8.4) — canonical names since P3.11; the old names are
        # 0.9.x deprecation aliases of the SAME classes.
        "CorrigendaError",
        "CorrectionError",
        "ParseError",
        "DuplicateIdError",  # P0-5 — additive, subclasses ParseError
        "ProposalValidationError",
        "ValidationError",
        "CorrectionAborted",
        # Ports
        "BaseProvider",
        "ModelCatalog",
        "PipelineObserver",
        "StructuredCompletionClient",
        # LLM contract (lazy — producers)
        "OUTPUT_JSON_SCHEMA",
        "SYSTEM_PROMPT",
        "sanitize_error",
        # Schemas (domain)
        "BlockManifest",
        "ChunkGranularity",
        "ChunkPlannerConfig",
        "CorrectionReport",
        "DocumentManifest",
        "GuardConfig",
        "HyphenRole",
        "LineManifest",
        "LineStatus",
        "LineTrace",
        "LineContext",
        "LineProposal",
        "LossPolicy",
        "ModelInfo",
        "PageManifest",
        "PairingPolicy",
        "RetryPolicy",
        "Usage",
        # Report v2 (§9, P3.5)
        "LineOutcome",
        "ProposalStage",
        "ProposalFeatures",
        "DecisionStage",
        "DecisionReason",
        "ProjectionStage",
        # Provenance (§11, P3.9)
        "ProducerProvenance",
        "RunProvenance",
        # token_realign sidecar (ROADMAP V3 Phase 1) — additive
        "SidecarEntry",
        # Confidence block (ROADMAP V3 Phase 1) — additive
        "ConfidencePolicy",
        "ConfidenceScorer",
        "HeuristicScorer",
        "LineConfidence",
        # QE + routing (ROADMAP V3 Phase 3) — additive
        "HeuristicQEScorer",
        "QEScorer",
        "RoutingDecision",
        "RoutingPolicy",
        "route_line",
        # Structured page image (ROADMAP V3 Phase 4) — additive
        "ImageAsset",
        "ImageRef",
        "ImageTransform",
        "PageImage",
    ]
)


def test_public_api_is_exactly_the_snapshot():
    assert sorted(corrigenda.__all__) == PUBLIC_API_1_0, (
        "corrigenda.__all__ drifted from the 1.0 snapshot. If deliberate, "
        "update PUBLIC_API_1_0 here AND document the change in CHANGELOG.md "
        "(a removal/rename is a MAJOR version bump)."
    )


def test_every_public_symbol_resolves():
    for name in corrigenda.__all__:
        obj = getattr(corrigenda, name)  # raises AttributeError on breakage
        assert obj is not None, name


def test_lazy_map_is_subset_of_public_api():
    from corrigenda import _LAZY

    unknown = set(_LAZY) - set(corrigenda.__all__)
    assert not unknown, f"lazy symbols not in __all__: {sorted(unknown)}"


# ---------------------------------------------------------------------------
# 3. Entry-point signatures + report keys
# ---------------------------------------------------------------------------


def _param_names(func) -> list[str]:
    return [p for p in inspect.signature(func).parameters if p != "self"]


def test_run_and_run_sync_signatures_are_pinned():
    expected = [
        "document_manifest",
        "source_files",
        "run_id",
        "should_abort",
        "page_images",
    ]
    assert _param_names(corrigenda.CorrectionPipeline.run) == expected
    assert _param_names(corrigenda.CorrectionPipeline.run_sync) == expected
    # §5.1 resorption — credentials must NEVER reappear on the run surface.
    for banned in ("api_key", "model", "provider_name"):
        assert banned not in expected


def test_for_provider_signature_is_pinned():
    params = _param_names(corrigenda.CorrectionPipeline.for_provider)
    assert params[0] == "provider"
    for required in ("api_key", "model", "provider_name", "observer"):
        assert required in params
    # ADR-011 slice D-fin — persistence left the engine surface for good.
    assert "output_writer" not in params
    assert "output_writer" not in _param_names(corrigenda.CorrectionPipeline.__init__)


def test_correction_report_json_keys_are_pinned():
    report = corrigenda.CorrectionReport(run_id="r")
    keys = set(report.model_dump().keys())
    assert keys == {
        "report_version",
        "run_id",
        "total_lines",
        "lines",
        "format_losses",
        "provenance",  # P3.9 — optional, additive (no version bump)
        "usage",  # ROADMAP V3 Phase 0 — optional, additive (no version bump)
        "sidecar",  # ROADMAP V3 Phase 1 — optional, additive (no version bump)
    }, (
        "CorrectionReport JSON shape moved — a key removal/rename requires "
        "bumping CORRECTION_REPORT_VERSION (§9); an addition must stay "
        "optional."
    )
    assert report.report_version == "2.0"
