# Remediation status — alto-llm-corrector

Last updated: 2026-05-25 (session L4)
Branch: `claude/vibrant-pascal-STfnR`

Roadmap reference: voir conversation (sections 5 et 6 du plan validé).
Convention : 1 session = 1 lot, même identifiant (L1 → L8).

## Progress

| Lot/Session | Statut       | Commits     | Notes |
|-------------|--------------|-------------|-------|
| L1          | done         | `f0270ed`   | size-limit + preset-app added |
| L2          | done         | `2148bcd`   | health/ready observation-only + off-loop |
| L3          | done         | `6218dd4`   | ProxyHeadersMiddleware + R2 decision documented |
| L4          | done         | (this push) | pipeline retry classification + event payload tests (+9 tests net) |
| L5          | not started  | —           | alto-core release readiness (B5, A5, B6, P3, P8) |
| L6          | not started  | —           | architecture cleanup (A1, A2, A3, A9) |
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

## In progress

- (none)

## Blocked

- (none)

## Remaining

- A5, B5, B6, P3, P8, A1, A2, A3, A9, P5, P6, P7, T1a, T1b, T1c, T1d, R3, R4, R5, A4, A6.

## New bugs discovered

- **NB1** — Pre-existing devDeps vulnerabilities surfaced during L1 install:
  - `postcss` < safe: XSS via unescaped `</style>` ([GHSA-qx2v-qp2m-jg93](https://github.com/advisories/GHSA-qx2v-qp2m-jg93)), moderate.
  - `vite` ≤ 6.4.1: path traversal + arbitrary file read via dev server WebSocket ([GHSA-4w7w-66w2-5vf9](https://github.com/advisories/GHSA-4w7w-66w2-5vf9), [GHSA-p9ff-h696-f583](https://github.com/advisories/GHSA-p9ff-h696-f583)), high.
  - These existed before L1 — not introduced by size-limit. Both are dev-only (`npm audit --omit=dev` reports 0). Proposed lot: new mini-lot **L9** (security bumps) after L8, or fold into L8 if scope permits. Decision pending.

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

## Tests count evolution

- Baseline (avant L1): 329 backend + 4 alto-core + 12 frontend = 345 total.
- Après L1: unchanged (345). L1 ne touche pas de tests.
- Après L2: 331 backend + 4 alto-core + 12 frontend = 347 total (+2).
- Après L3: 332 backend + 4 alto-core + 12 frontend = 348 total (+1).
- Après L4: 341 backend + 4 alto-core + 12 frontend = 357 total (+10 added, -1 deleted = +9 net).

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
