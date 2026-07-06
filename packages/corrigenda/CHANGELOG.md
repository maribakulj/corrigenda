# Changelog

All notable changes to **corrigenda** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### v1.0 normative corrections (SPECS_LIB_V2 §7) — in progress

- **F3** — the parser tolerates comments / processing-instructions among a
  `TextLine`'s children (they carry a callable `tag`); a trailing comment
  no longer aborts the whole file.
- **F5** — `_int_attr` parses float-valued coordinates (`"123.0"`, `"800.9"`)
  via `int(float(...))`, truncating toward zero. Non-numeric values still
  raise.
- **F6** *(byte change)* — slow-path token geometry: the 0.6 space weight now
  enters the total weight and rounding is spread by cumulative rounding.
  Widths still sum exactly to the line width; the final token only absorbs
  residual rounding instead of every space's accumulated deficit. Changes
  output bytes on slow-path lines with interior spaces (UNTOUCHED /
  SUBS_ONLY / FAST paths unaffected).
- **F13** — `GuardConfig` (frozen, injectable) gathers every anti-migration /
  acceptance threshold from the three guard stages; defaults reproduce the
  historical constants byte-for-byte. `FrozenPolicy.policy_fingerprint()`
  gives a stable hash for provenance (§11). Threaded through `check_line`,
  `check_adjacent_duplicates`, `reconcile_hyphen_pair`,
  `validate_llm_response`, and `CorrectionPipeline(guard_config=…)`.
- **F7** — `PairingPolicy` (frozen, injectable) makes hyphen pairing a seam;
  default reproduces the historical purely-sequential pairing. Forwarded
  through `build_document_manifest` / `parse_alto_file`.
- **F2** *(byte change)* — a changed `CONTENT` drops the now-stale `WC`/`CC`
  confidences (fast path, per changed String); the slow-path rebuild
  recycles only `ID` and `STYLEREFS` (§6.1 whitelist), inherits `VPOS`/
  `HEIGHT` from the line, recomputes `HPOS`/`WIDTH`, and never carries
  `WC`/`CC`/`SUBS_*`. Changes output bytes on slow-path lines and on
  fast-path Strings whose CONTENT changed.
- **F4** — the UNTOUCHED comparison strips both sides, matching the parser's
  `ocr_text` derivation; a line with a trailing `<SP/>` under identity
  correction now takes the UNTOUCHED path instead of being rewritten.
- **F9** — `RetryPolicy` (frozen, injectable) externalises the temperature
  ramp, attempt cap, backoff bases and per-chunk budget.
  `RetryPolicy.default()` reproduces the historical ramp (0.0/0.3/0.5, cap 3)
  to the byte; `RetryPolicy.deterministic()` sets every temperature to 0.
  `CorrectionPipeline(retry_policy=…)`.
- **F10** — `CorrectionPipeline.run(should_abort=…)` cooperative cancellation,
  probed between pages and chunks; raises `CorrectionAborted` (new
  `corrigenda.errors` module, `CorrectionError` root) before any output is
  written. In-flight provider calls are not interrupted.
- **F1** *(behaviour change on failure paths)* — a chunk whose retry budget is
  exhausted is re-planned one granularity finer (PAGE→BLOCK→WINDOW→LINE) and
  retried (`chunk_downgraded` event), bounded by `RetryPolicy.per_chunk_budget`
  (default 6). Only lines whose finest-grain chunk still fails fall back to OCR;
  a transient burst now recovers instead of reverting the whole chunk. New
  `chunk_downgraded` event added to the SSE contract.
- **F8** — overlapping windows distinguish *target* vs *context* lines
  (`ChunkRequest.target_line_ids`): each line is corrected in exactly one
  window (its best-following-context window), hyphen pairs kept together;
  overlaps become pure context. No effect on PAGE-granularity documents.
- **F14** *(pre-1.0 break)* — `BaseProvider.complete_structured` returns
  `(dict, Usage | None)`. New `Usage` model; `CorrectionResult.usage`
  aggregates the run; per-chunk tokens on the `chunk_completed` event.
- **Error hierarchy (§8.4)** — `corrigenda.errors`: `CorrectionError` root with
  `ParseError`, `ValidationError` (both also `ValueError`), `CorrectionAborted`;
  `HyphenIntegrityError` is now a `ValidationError`. `validate_llm_response`
  raises `ValidationError`.
- **CorrectionReport + dry-run (§9)** — the per-line trace is promoted to a
  public, versioned `CorrectionReport` (`report_version` "1.0"), returned on
  `CorrectionResult.report`. `run(apply=False)` runs the full pipeline
  (production, guards, reconciliation, in-memory rewrite) but never calls the
  `OutputWriter` — the report is the deliverable.
- **Provenance (§11)** — the corrected XML's `processingStep` now records the
  library version and a configuration fingerprint
  (`RetryPolicy`+`GuardConfig`+`ChunkPlannerConfig`) alongside provider/model.
- **py.typed + `mypy --strict` (F12/§8.3)** — PEP 561 marker shipped in the
  wheel; the package passes `mypy --strict` (new `corrigenda-types` CI job).
- **F12 (relocation)** — `Provider`, `JobStatus`, `JobManifest` (and its
  `images` map) moved to the backend (`app.schemas.job`); the vestigial
  `status` field was dropped from `PageManifest`/`DocumentManifest`. The core
  keeps only the domain enums (`LineStatus`, `ChunkGranularity`, `HyphenRole`,
  `PipelineEventType`). Top-level public surface is now 34 symbols.
- **F11** — the algorithm tests were repatriated into
  `packages/corrigenda/tests`; the package gates its own coverage (~86%, gate
  85%) and its CI job runs pytest with `--cov=corrigenda`.

### Renamed (§14 — pre-publication, no aliases)

- Distribution **alto-core → corrigenda**, import package **alto_core →
  corrigenda**. *Corrigenda* — the printed errata leaf bound into books —
  is literally what this library produces, carries the heritage domain,
  and survives the PAGE XML extension (v1.1) where "alto" would become a
  lie. Nothing was ever published under the old name, so there is no
  deprecation layer: final import paths from day one. The repository slug
  (URLs in project metadata) still reads alto-llm-corrector until the
  GitHub repository itself is renamed. The `processingStep` provenance
  brand written into corrected XML is now `corrigenda` (no effect on the
  byte-parity corpus: its files carry no `<Processing>` element).

### Post-audit corrective rounds (same release)

- **F1×F8 fixed** — the granularity descent re-plans a failed chunk's
  *target* lines only; context lines are no longer stolen from their own
  window and corrected at a finer grain.
- **F1×F10 fixed** — `should_abort` is probed inside the descent (before
  each sub-chunk) and `CorrectionAborted` is never converted into a
  `chunk_error` event.
- **F8 (spec letter)** — `validate_llm_response(target_line_ids=…)`: the
  1:1 count is enforced on targets; a missing context-line output is not an
  error. Per-entry structural checks stay strict; hyphen integrity runs
  over the target set. `None` keeps the historical exact-count contract.
- **`run_sync()` (§8.1)** — synchronous façade over `run()`; refuses a
  running event loop.
- **`ChunkPlannerConfig` frozen (§8.2)** — now a `FrozenPolicy` with
  `policy_fingerprint()`, like the other three policies.
- **Provenance fingerprint unified (§11)** — public
  `CorrectionPipeline.config_fingerprint()`, composed from the four
  policies' public `policy_fingerprint()` values (sorted-JSON sha256/16)
  and now covering `PairingPolicy` (provenance-only ctor param).
  Reproducible by consumers from the public API.
- **Slow-path SP geometry recomputed** *(byte change)* — SPs no longer
  recycle stale pre-correction HPOS/WIDTH; their geometry comes from the
  same `_compute_geometry` pass as the surrounding Strings (contiguous
  layout).
- **§6.1 whitelist extended with `STYLE`** — inline styling (bold/italics)
  is preserved on the slow path alongside `ID`/`STYLEREFS`. The spec names
  only the latter two, but its doctrine targets data *invalidated* by the
  text change — styling is not; dropping it destroyed real formatting on
  the non-regression corpus. Flagged for spec ratification.
- **F6 degenerate floor fixed** — the min-1 deficit is repaid across
  multiple donors; the exact-sum invariant survives every feasible width.
- **F7 cross-page gap** — `max_vertical_gap` is skipped for cross-page
  candidates (VPOS restarts per page).
- **F14 event semantics** — `chunk_completed` reports the chunk's total
  usage across all attempts, not just the final successful call.
- **Byte-parity gate (§13 DoD)** — `test_byte_parity_corpus.py` pins
  sha256 golden hashes of two deterministic scenarios over the corpus.
  Verified against the pre-v1.0 baseline (commit 8c4789c): identity
  corrections are BYTE-IDENTICAL; scripted corrections differ only on
  documented F2 (WC/CC) and F6/§6.1 (geometry) line classes.

### Changed
- **Retry policy on HTTP 4xx (other than 429) is now non-retryable.**
  The previous class-name allowlist (`exc.__class__.__name__ ==
  "HTTPStatusError"`) caused `401`, `403`, `404`, `422` to be retried
  3 times with exponential backoff — a waste, because client errors
  (bad API key, wrong model, schema rejection) don't heal on retry.
  The classifier now routes on `isinstance(exc,
  ProviderTransientError)`, and providers' HTTP wrapper deliberately
  leaves 4xx-non-429 errors un-wrapped, so they reach the classifier
  as non-retryable and the chunk falls back to OCR source on the
  first failure. Pinned by
  `test_pipeline_classifies_client_http_4xx_as_non_retryable`.
  `5xx`, `429`, and transport-level failures (timeout, network,
  protocol) retain the previous 3-attempt exponential-backoff
  behavior.
- Clarified in the `### Added` section of `[0.1.0a1]` which symbols
  are re-exported at the package root (`from corrigenda import …`)
  versus the ones that are sub-module-only. The technical contract
  is unchanged — every symbol previously listed remains importable
  from its canonical path. (roadmap L5 / B5)

### Added
- `ProviderTransientError.status_code: int | None` — when the
  underlying transport failure was an HTTP error, the originating
  status code is preserved on the wrapped exception so observers can
  route on 429 vs 503 vs 500 without parsing the message. `None` for
  transport-level failures (timeout, network, protocol). The full
  underlying exception remains reachable via `__cause__` for callers
  that need response headers or the request URL.

### Documentation
- Public Pydantic models (`LineManifest`, `DocumentManifest`,
  `BlockManifest`, `PageManifest`, `JobManifest`, `ChunkPlannerConfig`,
  `LLMLineInput`, `LLMLineOutput`, `ModelInfo`, `Coords`) and enums
  (`JobStatus`, `LineStatus`, `ChunkGranularity`, `Provider`,
  `HyphenRole`) now carry a one-line docstring. PyPI consumers get
  IDE help/intellisense out of the box. (roadmap L5 / A5)

### CI / Release
- Single source of truth for the smoke-import check:
  `packages/corrigenda/_smoke_imports.py` iterates `corrigenda.__all__`
  and is invoked by `.github/workflows/ci.yml`,
  `.github/workflows/publish-corrigenda.yml`, and
  `scripts/release-corrigenda.sh`. Drift between the three is now
  impossible. (roadmap L5 / B6)
- Added `Programming Language :: Python :: 3.13` classifier
  (`requires-python = ">=3.11"` already permitted 3.13). (roadmap L5 / P3)

## [0.1.0a1] — 2026-05-25

Initial alpha release.

### Added

> **Import paths.** Each section below documents the path the listed
> symbols live at. Most are sub-module imports, e.g.
> `from corrigenda.formats.alto.rewriter import RewriterMetrics`. The shorter
> set of names re-exported at the package root —
> `from corrigenda import CorrectionPipeline, BaseProvider, ...` — is
> defined exclusively by `corrigenda.__all__`. Symbols listed below
> that are NOT in `__all__` (e.g. `RewriterMetrics`, `ReconcileMetrics`,
> `plan_page`, `validate_llm_response`, `AcceptanceResult`, …) are
> sub-module-only: they remain importable from their canonical path,
> but `from corrigenda import RewriterMetrics` will raise `ImportError`.

- `corrigenda.formats.alto`: ALTO XML parsing and rewriting (v2/v3/v4), with
  the Hyphenation Reconciler.
  - `parse_alto_file`, `build_document_manifest` *(top-level)*
  - `rewrite_alto_file`, `extract_output_texts` *(top-level)*, `RewriterMetrics` *(sub-module only)*
  - `enrich_chunk_lines`, `reconcile_hyphen_pair`, `ReconcileMetrics`,
    `classify_reconcile_outcome`, `should_stay_in_same_chunk` *(all sub-module only)*
- `corrigenda.core`: chunk planning, LLM-response validation,
  per-line acceptance policy, and `CorrectionPipeline`.
  - `CorrectionPipeline`, `CorrectionResult`, `sanitize_error` *(top-level)*
  - `plan_page`, `downgrade_granularity` *(sub-module only)*
  - `validate_llm_response` *(sub-module only)*
  - `check_line`, `check_adjacent_duplicates`, `AcceptanceResult` *(all sub-module only)*
- `corrigenda.core.protocols`: ports consumers implement.
  - `BaseProvider`, `PipelineObserver`, `OutputWriter` *(top-level)*
  - `OUTPUT_JSON_SCHEMA`, `SYSTEM_PROMPT` *(top-level, home: `corrigenda.producers.llm`)*
- `corrigenda.core.schemas`: domain Pydantic models (manifests, enums, LLM
  payloads, traces, model info). Top-level re-exports cover the
  models consumers typically reach for — see `corrigenda.__all__`.

### Public API guarantees (alpha caveat)
- Importable via the top-level package: `from corrigenda import
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

[Unreleased]: https://github.com/maribakulj/alto-llm-corrector/compare/corrigenda-v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/maribakulj/alto-llm-corrector/releases/tag/corrigenda-v0.1.0a1
