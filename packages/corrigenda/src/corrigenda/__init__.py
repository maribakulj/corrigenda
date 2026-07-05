"""corrigenda — pure ALTO XML correction pipeline.

Sub-packages:

- :mod:`corrigenda.alto` — ALTO XML parsing/rewriting and the
  Hyphenation Reconciler.
- :mod:`corrigenda.pipeline` — chunk planning, validation, line
  acceptance, and the orchestrating :class:`CorrectionPipeline`.
- :mod:`corrigenda.schemas` — Pydantic models shared across the pipeline.
- :mod:`corrigenda.protocols` — ports (:class:`BaseProvider`,
  :class:`PipelineObserver`, :class:`OutputWriter`) consumers implement.

Top-level re-exports give a single import surface for the symbols most
consumers reach for. See the README for a minimal working example.
"""

from corrigenda.alto.parser import build_document_manifest, parse_alto_file
from corrigenda.alto.rewriter import extract_output_texts, rewrite_alto_file
from corrigenda.errors import (
    CorrectionAborted,
    CorrectionError,
    ParseError,
    ValidationError,
)
from corrigenda.pipeline.correction_pipeline import (
    CorrectionPipeline,
    CorrectionResult,
    sanitize_error,
)
from corrigenda.protocols import BaseProvider, OutputWriter, PipelineObserver
from corrigenda.protocols.provider import OUTPUT_JSON_SCHEMA, SYSTEM_PROMPT
from corrigenda.schemas import (
    BlockManifest,
    ChunkGranularity,
    ChunkPlannerConfig,
    CorrectionReport,
    DocumentManifest,
    GuardConfig,
    HyphenRole,
    LineManifest,
    LineStatus,
    LineTrace,
    LLMLineInput,
    LLMLineOutput,
    ModelInfo,
    PageManifest,
    PairingPolicy,
    RetryPolicy,
    Usage,
)

__version__ = "0.1.0a1"

__all__ = [
    # Parser / rewriter
    "build_document_manifest",
    "parse_alto_file",
    "extract_output_texts",
    "rewrite_alto_file",
    # Pipeline
    "CorrectionPipeline",
    "CorrectionResult",
    # Errors (§8.4)
    "CorrectionError",
    "ParseError",
    "ValidationError",
    "CorrectionAborted",
    # Ports
    "BaseProvider",
    "OutputWriter",
    "PipelineObserver",
    # LLM contract
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
    "LLMLineInput",
    "LLMLineOutput",
    "ModelInfo",
    "PageManifest",
    "PairingPolicy",
    "RetryPolicy",
    "Usage",
    # Version
    "__version__",
]
