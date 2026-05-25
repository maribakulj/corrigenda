# Remediation status — alto-llm-corrector

Last updated: 2026-05-25 (session L6)
Branch: `claude/vibrant-pascal-STfnR`

Roadmap reference: voir conversation (sections 5 et 6 du plan validé).
Convention : 1 session = 1 lot, même identifiant (L1 → L8).

## Progress

| Lot/Session | Statut       | Commits     | Notes |
|-------------|--------------|-------------|-------|
| L1          | done         | `f0270ed`   | size-limit + preset-app added |
| L2          | done         | `2148bcd`   | health/ready observation-only + off-loop |
| L3          | done         | `6218dd4`   | ProxyHeadersMiddleware + R2 decision documented |
| L4          | done         | `610ccce`   | pipeline retry classification + event payload tests (+9 tests net) |
| L5          | done         | `5412560`   | alto-core release readiness — docstrings, smoke unified, CHANGELOG clarified |
| L6          | done         | (this push) | architecture cleanup — 0 prod consumer of legacy run_job; double-lock removed |
| L7          | not started  | —           | release pipeline (P5, P6, P7) |
| L8          | not started  | —           | backlog (T1a-d, R3, R4, R5, A4, A6) — optional |

## Done

- **B1** — `size-limit` + `@size-limit/preset-app` added to `frontend/devDependencies` (L1, commit `f0270ed`). `npx size-limit` exits 0, reports JS 49.77 KB / 75 KB budget, CSS 3.88 KB / 10 KB budget.
- **B2** — `/health/ready` no longer mutates the filesystem (L2). Replaced `mkdir + write_bytes + unlink` with `path.is_dir() and os.access(path, os.W_OK)`. New test `test_health_ready_does_not_create_storage_dir` characterises the contract (failed against old code, passes against new).
- **B3** — `/health/ready` storage check runs off the event loop via `asyncio.to_thread` (L2). Even an `os.access` syscall can stall asyncio for tens of ms on NFS/overlay FS, freezing concurrent SSE streams. Wrapping in `to_thread` prevents that.
- Bonus coverage: new test `test_health_ready_returns_503_when_storage_not_writable` exercises the degraded branch end-to-end (uses a file-not-dir as `JOB_STORAGE_DIR` for deterministic failure regardless of test-runner user).
- **R1** — `ProxyHeadersMiddleware` added to `create_app()`, gated by env var `TRUSTED_PROXIES` (default `127.0.0.1` = dev-safe, both Dockerfiles override to `*`). Defence in depth: uvicorn keeps its own `--proxy-headers --forwarded-allow-ips=*` flags so the rewrite happens even if the Python middleware is mis-configured (both layers are idempotent). New test `test_rate_limit_uses_x_forwarded_for` proves slowapi now keys on the real caller IP (11 requests with 11 distinct `X-Forwarded-For` values all succeed — without the fix the 11th was 429).
- **R2** — middleware order documented as deliberate. Final stack: `CORS (outermost) → ProxyHeaders → SlowAPI → endpoint`. CORS stays outside SlowAPI on purpose: rate-limiting OPTIONS preflights would surface as opaque CORS errors in the browser the moment a user clicks faster than the cap. Preflights are cheap (no body, no DB, no LLM call), so the cost of not counting them is negligible compared to the UX hit. Documented in `backend/app/main.py` middleware block.
- **B4** — bidon test `test_create_job_endpoint_has_rate_limit_attached` deleted in L4. Three `or` conditions were each broad enough to pass on any decorated function. Wiring proof is now exclusively the two end-to-end tests `test_providers_models_rate_limit_blocks_after_threshold` + `test_rate_limit_uses_x_forwarded_for` (different endpoint but same Limiter + SlowAPIMiddleware stack — a regression in the wiring trips at least one).
- **T0a** — pipeline exception classification (3 branches) now covered (L4):
  - `test_pipeline_classifies_hyphen_violation_with_zero_backoff` proves `ValueError("hyphen_integrity_violation: …")` retries instantly the first time (backoff=0) and emits a retry event tagged with the fixed sentinel.
  - `test_pipeline_classifies_transient_http_with_exponential_backoff` proves duck-typed HTTPStatusError-like exceptions use backoff = attempt * 2.
  - `test_pipeline_classifies_llm_output_error_with_linear_backoff` proves generic `ValueError` / `JSONDecodeError` use backoff = attempt.
- **T0b** — event payload shape now covered (L4):
  - `test_chunk_error_event_payload_shape` (forced `_run_chunk` crash) verifies `chunk_id`, `message[:200]`, `exception_type` keys.
  - `test_hyphen_partner_missing_event_emitted_with_direction` (forced `_resolve_partner → None`) verifies `chunk_id`, `line_id`, `missing_partner_id`, `direction ∈ {backward, forward}`.
  - `test_retry_event_payload_shape` verifies `chunk_id`, `attempt: int >= 1`, `error: str` with `len <= 120`.
- **T0c** — `CompositeObserver` exception isolation now covered (L4): `test_composite_observer_isolates_failing_observer` + helpers for empty-list noop + registration-order verification (3 tests).
- **T0d** — multi-chunk persistent fallback now covered (L4): `test_persistent_failure_across_all_chunks_falls_back` proves status=COMPLETED + fallbacks ≥ 1 + every line's `corrected_text == ocr_text`.
- **A5** — 14 public Pydantic models (5 enums `JobStatus` / `LineStatus` / `ChunkGranularity` / `Provider` / `HyphenRole` + 9 BaseModels `Coords` / `LineManifest` / `BlockManifest` / `PageManifest` / `DocumentManifest` / `ChunkPlannerConfig` / `JobManifest` / `LLMLineInput` / `LLMLineOutput` / `ModelInfo`) now each carry a one-line docstring. PyPI consumers get IDE help/intellisense out of the box (L5).
- **B5** — `CHANGELOG.md` ### Added section restructured (L5). Each bullet is now tagged `*(top-level)*` or `*(sub-module only)*`. Lead note explicitly states: `from alto_core import RewriterMetrics` will raise `ImportError`; the canonical path is `from alto_core.alto.rewriter import RewriterMetrics`. New test `test_changelog_added_symbols_are_importable` pins the sub-module promise (parses the expected import-path map + asserts importability).
- **B6** — 3 divergent inline smoke scripts (ci.yml job alto-core-build, publish-alto-core.yml, scripts/release-alto-core.sh) replaced by calls to `packages/alto-core/_smoke_imports.py`. That single script iterates `alto_core.__all__` directly so any drift between the public API and the smoke check is now structurally impossible.
- **P3** — `Programming Language :: Python :: 3.13` classifier added to `packages/alto-core/pyproject.toml` (`requires-python = ">=3.11"` already permitted 3.13) (L5).
- **P8** — `test_top_level_public_api_is_importable` extended with the 5 missing symbols (`LineTrace`, `LLMLineInput`, `LLMLineOutput`, `ChunkPlannerConfig`, `LineStatus`). Plus new `test_all_matches_top_level_attrs` iterating `alto_core.__all__` directly so future additions are auto-checked (L5).
- **A1** — Production code (`app/api/jobs.py`) no longer imports `run_job` from the compat orchestrator (L6). Drives `JobRunner` + `FilesystemOutputWriter` directly. The 6 legacy test files (`test_orchestrator.py`, `test_orchestrator_snapshot.py`, `test_trace.py`, `test_integration.py`, `test_line_acceptance.py`, `test_api.py`) keep `from app.jobs.orchestrator import run_job` — that wrapper stays in place as the typed seam they expect. Verified: `grep "from app.jobs.orchestrator" backend/app/` returns 0.
- **A2** — `app/jobs/runner.py` imports `CorrectionPipeline` and `sanitize_error` directly from `alto_core` (top-level re-export) instead of going through the local `app.jobs.correction_pipeline` shim — backend native code no longer self-uses its own compat layer (L6).
- **A3** — `JobStore._remove_job` no longer re-acquires `self._lock` (L6). The caller (`_evict_stale` invoked from `create_job` under the lock) already holds it; the redundant `with self._lock:` violated the documented contract. Docstring tightened to reflect the invariant. RLock support for re-entrance preserved an invisible bug behind correct behaviour; removing it makes the contract enforceable by reading the code.
- **A9** — Reframed during L6: `app/jobs/correction_pipeline.py` is the only shim with an explicit `__all__`, so its `noqa: F401` was *redundant* — ruff's RUF100 rule removed it on commit. The audit had the asymmetry backwards: the *other 7 shims* are the ones that drift (they use `# noqa: F401` because they lack an explicit `__all__`). Promoting them to `__all__` would be the consistent fix, but it's a refactor outside L6's "correction minimale" scope. Closing A9 with the observation that no change is needed on `correction_pipeline.py` (it already uses the better pattern). A new follow-up `NB2` is logged below.

## In progress

- (none)

## Blocked

- (none)

## Remaining

- P5, P6, P7, T1a, T1b, T1c, T1d, R3, R4, R5, A4, A6.

## New bugs discovered

- **NB1** — Pre-existing devDeps vulnerabilities surfaced during L1 install:
  - `postcss` < safe: XSS via unescaped `</style>` ([GHSA-qx2v-qp2m-jg93](https://github.com/advisories/GHSA-qx2v-qp2m-jg93)), moderate.
  - `vite` ≤ 6.4.1: path traversal + arbitrary file read via dev server WebSocket ([GHSA-4w7w-66w2-5vf9](https://github.com/advisories/GHSA-4w7w-66w2-5vf9), [GHSA-p9ff-h696-f583](https://github.com/advisories/GHSA-p9ff-h696-f583)), high.
  - These existed before L1 — not introduced by size-limit. Both are dev-only (`npm audit --omit=dev` reports 0). Proposed lot: new mini-lot **L9** (security bumps) after L8, or fold into L8 if scope permits. Decision pending.
- **NB2** (L6) — 7 of the 8 backend shims (`app/alto/parser.py`, `rewriter.py`, `hyphenation.py`, `_norm.py`, `app/jobs/chunk_planner.py`, `validator.py`, `line_acceptance.py`) use `# noqa: F401  re-export` instead of declaring an explicit `__all__` like `correction_pipeline.py` does. The latter is the better pattern: it makes the re-export surface a first-class object linters can reason about, and it's robust against ruff's RUF100 (which strips redundant `noqa` comments). Migrating the 7 stragglers is a tiny, mechanical refactor — proposed for L8 if scope permits, otherwise a separate housekeeping commit.

## Tests added

- L1: none (packaging fix).
- L2:
  - `test_health_ready_does_not_create_storage_dir` (B2 characterisation — failed against old code).
  - `test_health_ready_returns_503_when_storage_not_writable` (degraded-branch end-to-end coverage, previously missing).
- L3:
  - `test_rate_limit_uses_x_forwarded_for` (R1 characterisation — failed against old code: 11th request from a distinct IP was rate-limited because slowapi saw `testclient` for all 11).
- L4 (+10 added, -1 removed = +9 net):
  - `test_pipeline_classifies_hyphen_violation_with_zero_backoff` (T0a, branch 1/3).
  - `test_pipeline_classifies_transient_http_with_exponential_backoff` (T0a, branch 2/3).
  - `test_pipeline_classifies_llm_output_error_with_linear_backoff` (T0a, branch 3/3).
  - `test_chunk_error_event_payload_shape` (T0b).
  - `test_hyphen_partner_missing_event_emitted_with_direction` (T0b).
  - `test_retry_event_payload_shape` (T0b).
  - `test_persistent_failure_across_all_chunks_falls_back` (T0d).
  - `test_composite_observer_isolates_failing_observer` (T0c, primary).
  - `test_composite_observer_with_no_observers_is_a_noop` (T0c, defensive).
  - `test_composite_observer_calls_observers_in_registration_order` (T0c, order contract).
  - DELETED `test_create_job_endpoint_has_rate_limit_attached` (B4, bidon).
- L5 (+2 alto-core tests):
  - `test_all_matches_top_level_attrs` (P8 — iterates `__all__`, future-proof).
  - `test_changelog_added_symbols_are_importable` (B5 — pins sub-module import-path promises).

## Tests count evolution

- Baseline (avant L1): 329 backend + 4 alto-core + 12 frontend = 345 total.
- Après L1: unchanged (345). L1 ne touche pas de tests.
- Après L2: 331 backend + 4 alto-core + 12 frontend = 347 total (+2).
- Après L3: 332 backend + 4 alto-core + 12 frontend = 348 total (+1).
- Après L4: 341 backend + 4 alto-core + 12 frontend = 357 total (+10 added, -1 deleted = +9 net).
- Après L5: 341 backend + 6 alto-core + 12 frontend = 359 total (+2 alto-core).

## Coverage evolution

- Baseline `observers.py`: 0% (audit).
- Baseline `correction_pipeline.py`: ~70% (audit).
- Cible post-L4: `observers.py` ≥ 60%, `correction_pipeline.py` ≥ 85%.
- Post-L4 actual : see `pytest --cov=app --cov=alto_core --cov-report=term-missing` when needed; observers.py covered by 3 direct tests + indirect via CompositeObserver in JobRunner; classification branches (`is_hyphen_violation` / `is_transient_http` / `is_llm_output_error`) and event payload sites are now reached by at least one assertion each.
- Cible post-L8: `observers.py` ≥ 90%, `store.py` ≥ 95%, `health.py` ≥ 95%.

## Risks remaining

- L6 refactor pourrait casser des tests qui patchent `run_job` — mitigation prévue : grep préalable.
- L7 check tag peut bloquer une release légitime si workflow lancé avant push tag — mitigation : doc CONTRIBUTING.
- NB1 (vite/postcss) : dev-only mais à traiter avant fin de remédiation pour ne pas laisser de signal rouge sur `npm audit`.

## Decisions pending (utilisateur)

- A4: garder ou supprimer le legacy `/health` endpoint ? (revue en L8)
- A6: déplacer les 6 tests backend qui importent privés alto-core vers `packages/alto-core/tests/`, ou promouvoir les privés ? (revue en L8)
- L8: faire ou reporter ?
- NB1: créer un lot L9 dédié ou folder dans L8 ?
