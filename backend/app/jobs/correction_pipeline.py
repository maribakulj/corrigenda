"""Backward-compat shim. Implementation lives in :mod:`alto_core.pipeline.correction_pipeline`.

New code should import from `alto_core.pipeline.correction_pipeline` directly. This module exists
so that the existing `from app.jobs.correction_pipeline import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.pipeline.correction_pipeline import (  # noqa: F401  re-export
    _SECRET_RE,
    OUTPUT_JSON_SCHEMA,
    SYSTEM_PROMPT,
    BaseProvider,
    ChunkPlannerConfig,
    ChunkRequest,
    CorrectionPipeline,
    CorrectionResult,
    DocumentManifest,
    HyphenRole,
    JobStatus,
    JobTrace,
    LineManifest,
    LineStatus,
    LineTrace,
    LLMUserPayload,
    OutputWriter,
    PageManifest,
    Path,
    PipelineObserver,
    _build_hyphen_pairs,
    _reconcile_one_pair,
    _resolve_partner,
    _trace_key,
    asyncio,
    check_adjacent_duplicates,
    check_line,
    dataclass,
    enrich_chunk_lines,
    extract_output_texts,
    logger,
    logging,
    plan_page,
    re,
    reconcile_hyphen_pair,
    rewrite_alto_file,
    sanitize_error,
    validate_llm_response,
)
