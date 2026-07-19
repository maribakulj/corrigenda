"""corrigenda — structure-safe post-OCR correction of heritage transcriptions.

Sub-packages (§3 tree):

- :mod:`corrigenda.core` — pure algorithms: schemas, guards, hyphenation
  reconciliation, chunk planning, response validation, the orchestrating
  :class:`CorrectionPipeline`, and every port (zero I/O, zero lxml —
  enforced by the import-contract test).
- :mod:`corrigenda.formats` — concrete transcription formats. Today
  :mod:`corrigenda.formats.alto` (hardened parser + 4-path rewriter);
  PAGE XML plugs in the same seam.
- :mod:`corrigenda.producers` — edition-producer surfaces (the LLM
  system prompt + JSON output schema today).

Top-level re-exports give a single import surface. Format- and
producer-bound symbols are exposed LAZILY (PEP 562) so that importing
:mod:`corrigenda` from a core-only consumer never loads lxml.
"""

from typing import TYPE_CHECKING, Any

from corrigenda.core.decisions import (
    DecisionSet,
    LineDecision,
)
from corrigenda.core.identity import LineRef
from corrigenda.core.editing import (
    EditOp,
    EditRejection,
    EditResult,
    EditScript,
    MatchAnchor,
    RangeAnchor,
    ReplaceLine,
    ReplaceSpan,
    apply_edit_script,
    normalize_anchor,
)
from corrigenda.core.pipeline import (
    CorrectionPipeline,
    CorrectionResult,
    sanitize_error,
)
from corrigenda.core.protocols import (
    BaseProvider,
    EditProducer,
    ModelCatalog,
    PipelineObserver,
    ProducerMetadata,
    ProducerOptions,
    StructuredCompletionClient,
    require_page_images,
)
from corrigenda.core.schemas import (
    BlockManifest,
    ChunkGranularity,
    ChunkPlannerConfig,
    CorrectionReport,
    DecisionReason,
    DecisionStage,
    DocumentManifest,
    GuardConfig,
    HyphenRole,
    LineManifest,
    LineOutcome,
    LineStatus,
    LineTrace,
    LineContext,
    LineProposal,
    LossPolicy,
    ModelInfo,
    PageManifest,
    PairingPolicy,
    ProducerProvenance,
    ProjectionStage,
    ProposalFeatures,
    ProposalStage,
    RetryPolicy,
    RunProvenance,
    Usage,
)
from corrigenda.errors import (
    CorrectionAborted,
    CorrectionError,
    DuplicateIdError,
    ParseError,
    ValidationError,
)

if TYPE_CHECKING:  # typed view of the lazy symbols below
    from corrigenda.formats.alto.parser import (
        build_document_manifest as build_document_manifest,
    )
    from corrigenda.formats.alto.parser import parse_alto_file as parse_alto_file
    from corrigenda.formats.alto.rewriter import (
        extract_output_texts as extract_output_texts,
    )
    from corrigenda.formats.alto.rewriter import (
        rewrite_alto_file as rewrite_alto_file,
    )
    from corrigenda.formats.alto.adapter import (
        AltoFormatAdapter as AltoFormatAdapter,
    )
    from corrigenda.formats.page.adapter import (
        PageFormatAdapter as PageFormatAdapter,
    )
    from corrigenda.formats.page.parser import parse_page_file as parse_page_file
    from corrigenda.formats.page.rewriter import (
        rewrite_page_file as rewrite_page_file,
    )
    from corrigenda.integrations.llm import (
        OUTPUT_JSON_SCHEMA as OUTPUT_JSON_SCHEMA,
    )
    from corrigenda.integrations.llm import SYSTEM_PROMPT as SYSTEM_PROMPT
    from corrigenda.producers.llm_edit import LLMEditProducer as LLMEditProducer
    from corrigenda.producers.rules import RulesProducer as RulesProducer
    from corrigenda.producers.rules import SubstitutionRule as SubstitutionRule
    from corrigenda.producers.rules import (
        default_french_ocr_rules as default_french_ocr_rules,
    )

__version__ = "0.9.0"

# MAINTAINER NOTE — adding/removing a PUBLIC symbol touches THREE lists here,
# by design (the friction is a feature: a public API change should be
# deliberate, and ``tests/test_public_api_snapshot.py`` fails until all three
# agree):
#   1. either an eager ``from corrigenda.core... import`` above (pure-core
#      symbols, no lxml) OR the ``_LAZY`` map below (format/producer symbols
#      that must stay lazy so ``import corrigenda`` never loads lxml);
#   2. the ``TYPE_CHECKING`` block above, if the symbol is lazy (so mypy/IDEs
#      still see it);
#   3. ``__all__`` at the bottom.
#
#: Lazily resolved top-level names -> their home module (PEP 562). These
#: pull in lxml (formats) or producer surfaces, so they materialise only
#: on first attribute access — `import corrigenda` alone stays pure.
_LAZY: dict[str, str] = {
    "build_document_manifest": "corrigenda.formats.alto.parser",
    "parse_alto_file": "corrigenda.formats.alto.parser",
    "extract_output_texts": "corrigenda.formats.alto.rewriter",
    "rewrite_alto_file": "corrigenda.formats.alto.rewriter",
    "AltoFormatAdapter": "corrigenda.formats.alto.adapter",
    "PageFormatAdapter": "corrigenda.formats.page.adapter",
    "parse_page_file": "corrigenda.formats.page.parser",
    "rewrite_page_file": "corrigenda.formats.page.rewriter",
    "OUTPUT_JSON_SCHEMA": "corrigenda.integrations.llm",
    "SYSTEM_PROMPT": "corrigenda.integrations.llm",
    "LLMEditProducer": "corrigenda.producers.llm_edit",
    "RulesProducer": "corrigenda.producers.rules",
    "SubstitutionRule": "corrigenda.producers.rules",
    "default_french_ocr_rules": "corrigenda.producers.rules",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module 'corrigenda' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    # Parser / rewriter (lazy — formats)
    "build_document_manifest",
    "parse_alto_file",
    "extract_output_texts",
    "rewrite_alto_file",
    "parse_page_file",
    "rewrite_page_file",
    "AltoFormatAdapter",
    "PageFormatAdapter",
    # Pipeline
    "CorrectionPipeline",
    "CorrectionResult",
    # Decisions (ADR-011) — the run's outcome, read off the result
    "DecisionSet",
    "LineDecision",
    "LineRef",
    # Edit protocol (§4) — pure core
    "EditScript",
    "EditOp",
    "ReplaceLine",
    "ReplaceSpan",
    "MatchAnchor",
    "RangeAnchor",
    "EditResult",
    "EditRejection",
    "apply_edit_script",
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
    # Errors (§8.4)
    "CorrectionError",
    "ParseError",
    "DuplicateIdError",
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
    # Schemas (domain only)
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
    # Version
    "__version__",
]
