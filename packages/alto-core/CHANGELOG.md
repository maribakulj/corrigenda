# Changelog

All notable changes to **alto-core** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Clarified in the `### Added` section of `[0.1.0a1]` which symbols
  are re-exported at the package root (`from alto_core import …`)
  versus the ones that are sub-module-only. The technical contract
  is unchanged — every symbol previously listed remains importable
  from its canonical path. (roadmap L5 / B5)

### Documentation
- Public Pydantic models (`LineManifest`, `DocumentManifest`,
  `BlockManifest`, `PageManifest`, `JobManifest`, `ChunkPlannerConfig`,
  `LLMLineInput`, `LLMLineOutput`, `ModelInfo`, `Coords`) and enums
  (`JobStatus`, `LineStatus`, `ChunkGranularity`, `Provider`,
  `HyphenRole`) now carry a one-line docstring. PyPI consumers get
  IDE help/intellisense out of the box. (roadmap L5 / A5)

### CI / Release
- Single source of truth for the smoke-import check:
  `packages/alto-core/_smoke_imports.py` iterates `alto_core.__all__`
  and is invoked by `.github/workflows/ci.yml`,
  `.github/workflows/publish-alto-core.yml`, and
  `scripts/release-alto-core.sh`. Drift between the three is now
  impossible. (roadmap L5 / B6)
- Added `Programming Language :: Python :: 3.13` classifier
  (`requires-python = ">=3.11"` already permitted 3.13). (roadmap L5 / P3)

## [0.1.0a1] — 2026-05-25

Initial alpha release.

### Added

> **Import paths.** Each section below documents the path the listed
> symbols live at. Most are sub-module imports, e.g.
> `from alto_core.alto.rewriter import RewriterMetrics`. The shorter
> set of names re-exported at the package root —
> `from alto_core import CorrectionPipeline, BaseProvider, ...` — is
> defined exclusively by `alto_core.__all__`. Symbols listed below
> that are NOT in `__all__` (e.g. `RewriterMetrics`, `ReconcileMetrics`,
> `plan_page`, `validate_llm_response`, `AcceptanceResult`, …) are
> sub-module-only: they remain importable from their canonical path,
> but `from alto_core import RewriterMetrics` will raise `ImportError`.

- `alto_core.alto`: ALTO XML parsing and rewriting (v2/v3/v4), with
  the Hyphenation Reconciler.
  - `parse_alto_file`, `build_document_manifest` *(top-level)*
  - `rewrite_alto_file`, `extract_output_texts` *(top-level)*, `RewriterMetrics` *(sub-module only)*
  - `enrich_chunk_lines`, `reconcile_hyphen_pair`, `ReconcileMetrics`,
    `classify_reconcile_outcome`, `should_stay_in_same_chunk` *(all sub-module only)*
- `alto_core.pipeline`: chunk planning, LLM-response validation,
  per-line acceptance policy, and `CorrectionPipeline`.
  - `CorrectionPipeline`, `CorrectionResult`, `sanitize_error` *(top-level)*
  - `plan_page`, `downgrade_granularity` *(sub-module only)*
  - `validate_llm_response` *(sub-module only)*
  - `check_line`, `check_adjacent_duplicates`, `AcceptanceResult` *(all sub-module only)*
- `alto_core.protocols`: ports consumers implement.
  - `BaseProvider`, `PipelineObserver`, `OutputWriter` *(top-level)*
  - `OUTPUT_JSON_SCHEMA`, `SYSTEM_PROMPT` *(top-level, on `alto_core.protocols.provider`)*
- `alto_core.schemas`: domain Pydantic models (manifests, enums, LLM
  payloads, traces, model info). Top-level re-exports cover the
  models consumers typically reach for — see `alto_core.__all__`.

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
