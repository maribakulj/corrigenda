# Changelog

All notable changes to **alto-core** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0a1] — 2026-05-25

Initial alpha release.

### Added
- `alto_core.alto`: ALTO XML parsing and rewriting (v2/v3/v4), with
  the Hyphenation Reconciler.
  - `parse_alto_file`, `build_document_manifest`
  - `rewrite_alto_file`, `extract_output_texts`, `RewriterMetrics`
  - `enrich_chunk_lines`, `reconcile_hyphen_pair`, `ReconcileMetrics`,
    `classify_reconcile_outcome`, `should_stay_in_same_chunk`
- `alto_core.pipeline`: chunk planning, LLM-response validation,
  per-line acceptance policy, and `CorrectionPipeline`.
  - `CorrectionPipeline`, `CorrectionResult`, `sanitize_error`
  - `plan_page`, `downgrade_granularity`
  - `validate_llm_response`
  - `check_line`, `check_adjacent_duplicates`, `AcceptanceResult`
- `alto_core.protocols`: ports consumers implement.
  - `BaseProvider` (with `OUTPUT_JSON_SCHEMA` + `SYSTEM_PROMPT`)
  - `PipelineObserver`, `OutputWriter`
- `alto_core.schemas`: domain Pydantic models (manifests, enums, LLM
  payloads, traces, model info).

### Public API guarantees (alpha caveat)
- Importable via the top-level package: `from alto_core import
  CorrectionPipeline, BaseProvider, parse_alto_file, …` (full list
  in the package `__all__`).
- Each sub-module declares its own `__all__`.
- ARCHITECTURE.md ADR-006: the pipeline never logs by itself — every
  diagnostic is an `observer.on_event(...)` so hosts route them as
  they wish.
- Snapshot tests on a 566-line corpus pin byte-identical rewrite
  output across the 35+ commits of the refactor that produced this
  release.

### Known limitations
- API is still alpha; breaking changes possible until 1.0.
- `CorrectionPipeline.run` accepts `provider_name`/`model`/`api_key`
  individually (server-side legacy); a future release will likely fold
  them into the injected `BaseProvider`.

[Unreleased]: https://github.com/maribakulj/alto-llm-corrector/compare/alto-core-v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/maribakulj/alto-llm-corrector/releases/tag/alto-core-v0.1.0a1
