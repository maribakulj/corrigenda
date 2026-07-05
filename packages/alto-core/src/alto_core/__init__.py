"""alto-core — pure ALTO XML correction pipeline.

Sub-packages:

- :mod:`alto_core.alto` — ALTO XML parsing/rewriting and the
  Hyphenation Reconciler.
- :mod:`alto_core.pipeline` — chunk planning, validation, line
  acceptance, and the orchestrating :class:`CorrectionPipeline`.
- :mod:`alto_core.schemas` — Pydantic models shared across the pipeline.
- :mod:`alto_core.protocols` — ports (:class:`BaseProvider`,
  :class:`PipelineObserver`, :class:`OutputWriter`) consumers implement.

Top-level re-exports give a single import surface for the symbols most
consumers reach for. See the README for a minimal working example.
"""

from alto_core.alto.parser import build_document_manifest, parse_alto_file
from alto_core.alto.rewriter import extract_output_texts, rewrite_alto_file
from alto_core.pipeline.correction_pipeline import (
    CorrectionPipeline,
    CorrectionResult,
    sanitize_error,
)
from alto_core.protocols import BaseProvider, OutputWriter, PipelineObserver
from alto_core.protocols.provider import OUTPUT_JSON_SCHEMA, SYSTEM_PROMPT
from alto_core.schemas import (
    BlockManifest,
    ChunkGranularity,
    ChunkPlannerConfig,
    DocumentManifest,
    GuardConfig,
    HyphenRole,
    JobManifest,
    JobStatus,
    LineManifest,
    LineStatus,
    LineTrace,
    LLMLineInput,
    LLMLineOutput,
    ModelInfo,
    PageManifest,
    PairingPolicy,
    Provider,
    RetryPolicy,
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
    "DocumentManifest",
    "GuardConfig",
    "HyphenRole",
    "JobManifest",
    "JobStatus",
    "LineManifest",
    "LineStatus",
    "LineTrace",
    "LLMLineInput",
    "LLMLineOutput",
    "ModelInfo",
    "PageManifest",
    "PairingPolicy",
    "Provider",
    "RetryPolicy",
    # Version
    "__version__",
]
