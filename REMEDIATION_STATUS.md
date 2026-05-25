# Remediation status — alto-llm-corrector

Last updated: 2026-05-25 (post-L8 corrective wave)
Branch: `claude/vibrant-pascal-STfnR`

Roadmap reference: voir conversation (sections 5 et 6 du plan validé).
Convention : 1 session = 1 lot, même identifiant (L1 → L8).
A final audit (`option (c)`) ran after L8 and surfaced 8 P0/P1/P2 items
introduced by the L1→L8 commits themselves. They are documented in
section **Corrective wave (post-L8 audit)** below.

## Progress

| Lot/Session | Statut       | Commits     | Notes |
|-------------|--------------|-------------|-------|
| L1          | done         | `f0270ed`   | size-limit + preset-app added |
| L2          | done         | `2148bcd`   | health/ready observation-only + off-loop |
| L3          | done         | `6218dd4`   | ProxyHeadersMiddleware + R2 decision documented |
| L4          | done         | `610ccce`   | pipeline retry classification + event payload tests (+9 tests net) |
| L5          | done         | `5412560`   | alto-core release readiness — docstrings, smoke unified, CHANGELOG clarified |
| L6          | done         | `f1af8e4`   | architecture cleanup — 0 prod consumer of legacy run_job; double-lock removed |
| L7          | done         | `0c94d6f`   | release pipeline hardening — version coherence, tag gating, anti-double-upload |
| L8          | partial      | (this push) | T1a + T1c (closed as redundant) + T1d + NB2 done; T1b/R3/R4/R5/A4/A6 deferred with rationale |

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
- **P5** — `ci.yml` job `alto-core-lint` gains a `Version coherence` step (L7). Reads `__version__` from `src/alto_core/__init__.py` via Python regex and grep's the CHANGELOG for a matching `## [X.Y.Z]` heading. CI blocks the merge if they diverge. Pre-L7 the check only fired at release-script time (`scripts/release-alto-core.sh:55-59`); now caught at every push.
- **P6** — `publish-alto-core.yml` gains a `Verify HEAD is on an alto-core release tag` step before build (L7). Requires an `alto-core-vX.Y.Z` tag pointing at HEAD where X.Y.Z matches `__version__`. Without it, a maintainer could accidentally workflow_dispatch on any default-branch SHA and publish whatever's there. `actions/checkout@v4` now uses `fetch-depth: 0 + fetch-tags: true` so the tag refs are in the local clone.
- **P7** — `scripts/release-alto-core.sh` gains an index-side guard before `twine upload` (L7). Hits `https://{,test.}pypi.org/pypi/alto-core/json`, lists existing versions, aborts with a clear message if `${VERSION}` is already there. Branches cleanly on 200 (duplicate check), 404 (first release), and network failure (warning, proceed). Pre-L7 a duplicate upload surfaced as an opaque twine 403 after the full build.
- **T1a** — `LoggingObserver` level mapping now covered by 2 new tests in `test_observers.py` (L8). `test_logging_observer_routes_warning_events_to_warning_level` proves the 3 event types in `_WARNING_EVENTS` (`warning`, `chunk_error`, `hyphen_partner_missing`) reach WARNING level; `test_logging_observer_routes_lifecycle_events_to_debug_level` proves the 6 lifecycle event types (`page_started`, `chunk_planned`, `chunk_started`, `chunk_completed`, `page_completed`, `retry`) stay at DEBUG.
- **T1c** — Closed as redundant in L8: already covered by L2's `test_health_ready_returns_503_when_storage_not_writable` in `test_health_and_rate_limit.py:77`. No new test needed.
- **T1d** — `non_hyphen_string_contents_preserved` fact added to `_structural_facts()` in `test_orchestrator_snapshot.py` (L8). Pins the contract that under identity correction, `<String CONTENT="...">` round-trips byte-for-byte on all non-`SUBS_TYPE` String elements. Exercised on both sample.xml (10 lines) and X0000002.xml (566 lines). Hyphen-pair Strings legitimately mutate (HYP reconstruction) and are excluded — the HYP-count assert already guards that path.
- **NB2** — 7 backend shims (`alto/{parser,rewriter,hyphenation,_norm}.py`, `jobs/{chunk_planner,validator,line_acceptance}.py`) migrated from `# noqa: F401  re-export` to explicit `__all__` declarations (L8). Matches the existing `app/jobs/correction_pipeline.py` pattern; ruff stays green (the `__all__` opts in to re-export semantics without needing a `noqa` suppression).

## In progress

- (none)

## Blocked

- (none)

## Deferred from L8 (require design decisions or carry CI risk)

- **T1b** (`test_jobstore_threadsafe_under_contention`): meaningful only if the production server moves off single-worker asyncio. Stress tests around `threading.Thread` + `concurrent.futures` tend to be flaky under CI load. Recommendation: skip until either (a) we migrate to multi-worker or (b) an actual race is reported. The `RLock` is documented as *defensive* — its absence wouldn't break anything today.
- **R3** (SSE race: terminal event lands between `job.status` fast-path check and `subscribe()`): would need a small re-shape of `stream_events` to subscribe FIRST then re-check status. Behaviour change (window narrowed but not closed). Defer until a real client-side miss is reported.
- **R4** (`JobStore.get_job` returns mutable `JobManifest`): would need to deep-copy or freeze the returned object. Touches every caller. Behaviour change. Defer; under asyncio single-threaded today, the window doesn't matter.
- **R5** (`JsonFormatter` double-encodes each extra field via `json.dumps`): perf smell, not a bug. Measurable cost only at very high log volumes. Recommendation: ignore until the log pipeline becomes a bottleneck (HF Spaces is currently nowhere near that).
- **A4** (3 health endpoints — `/health`, `/health/live`, `/health/ready`): keeping `/health` as the lightweight HF Spaces ping is intentional. The "redundancy" is documented in code. Recommendation: leave as is; no operator confusion has been reported and removing it would risk breaking the HF probe configured years ago.
- **A6** (6 backend tests import `_` privates from `alto_core.alto.parser` / `rewriter` / `hyphenation`): moving them into `packages/alto-core/tests/` requires duplicating the sample fixtures and would block alto-core from renaming its internals. The current setup couples backend tests to alto-core privates — manageable while the backend is the primary consumer. Recommendation: revisit when a second alto-core consumer appears.

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
- L8 (+2 backend tests, +1 fact extended on snapshot helper):
  - `test_logging_observer_routes_warning_events_to_warning_level` (T1a, warning side).
  - `test_logging_observer_routes_lifecycle_events_to_debug_level` (T1a, debug side).
  - `_structural_facts` extended with `non_hyphen_string_contents_preserved` + asserted in both `test_semantic_sample_xml` and `test_semantic_x0000002_xml` (T1d).

## Tests count evolution

- Baseline (avant L1): 329 backend + 4 alto-core + 12 frontend = 345 total.
- Après L1: unchanged (345). L1 ne touche pas de tests.
- Après L2: 331 backend + 4 alto-core + 12 frontend = 347 total (+2).
- Après L3: 332 backend + 4 alto-core + 12 frontend = 348 total (+1).
- Après L4: 341 backend + 4 alto-core + 12 frontend = 357 total (+10 added, -1 deleted = +9 net).
- Après L5: 341 backend + 6 alto-core + 12 frontend = 359 total (+2 alto-core).
- Après L7: unchanged (359). L7 ne touche pas de tests.
- Après L8: 343 backend + 6 alto-core + 12 frontend = 361 total (+2 backend).

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
- NB1: créer un lot L9 dédié pour `vite`/`postcss` devDeps bumps ?

## Corrective wave (post-L8 audit)

After L8 a final audit ("option (c)") surfaced 8 issues introduced by
the L1→L8 commits themselves. The wave applied each fix in its own
commit so it can be reverted independently.

| Priority | Commit       | Issue       | Title |
|----------|--------------|-------------|-------|
| P0       | `baeb2a1`    | B-NEW-1     | fix(api): look up `_JOB_TIMEOUT_SECONDS` dynamically (L6 regression) |
| P0       | `49b2249`    | B-NEW-2     | fix(release): version regex tolerates type-annotated `__version__` |
| P0       | `4dbd33c`    | B-NEW-4     | test(api): cover rate-limit wiring on POST /api/jobs |
| P1       | `b0c7b4d`    | B-NEW-3     | fix(deploy): drop redundant uvicorn `--proxy-headers` flag |
| P1       | `16742aa`    | T-FLAKY-1   | test(observers): use presence checks instead of strict counts |
| P2       | `c6a361a`    | S1          | test(store): pin the `_remove_job` locking contract |
| P2       | `618be08`    | S6          | chore(alto): remove unused private shims `_norm.py` and `_ns.py` |
| P2       | `b601b8a`    | S7          | ci: matrix alto-core CI across Python 3.11/3.12/3.13 |

### Items closed

- **B-NEW-1** — `app/api/jobs.py` line 18 was `from app.jobs.orchestrator import _JOB_TIMEOUT_SECONDS`. That snapshots the value at module import, so a test (or future env-driven hot-tune) that patches `app.jobs.orchestrator._JOB_TIMEOUT_SECONDS` had no effect on the actual timeout used by JobRunner. Switched to `from app.jobs import orchestrator as _orch` + `timeout_seconds=_orch._JOB_TIMEOUT_SECONDS` at the call site. Regression test `test_create_job_resolves_timeout_seconds_dynamically` patches the sentinel to `4242` and asserts the value reaches `JobRunner.run` via the POST /api/jobs route. Confirmed: against the pre-fix code the test captures `1800` instead of `4242` and fails as designed.
- **B-NEW-2** — The version-extraction regex `r"__version__\s*=\s*..."` used by `ci.yml`, `publish-alto-core.yml`, and `scripts/release-alto-core.sh` returned `None` on a perfectly legitimate type-annotated declaration like `__version__: Final[str] = "X.Y.Z"`, silently aborting the release pipeline. Added an optional `(?::\s*[^=]+)?` clause to all three regexes. Backed by `backend/tests/test_release_tooling.py` with 4 tests (canonical pattern against 5 variants + a `(?::` sentinel grep on each of the three tooling files so removal of the clause trips the test immediately).
- **B-NEW-3** — Both Dockerfiles passed `--proxy-headers --forwarded-allow-ips=*` to uvicorn in addition to the Python `ProxyHeadersMiddleware` installed in `create_app()`. The uvicorn flag hardcodes `*` and bypasses the configurable `TRUSTED_PROXIES` env var, so its presence effectively neutralised the deliberate trust filter. Removed the uvicorn flag from both `Dockerfile` and `backend/Dockerfile`. The Python middleware remains as the single source of truth. Refreshed the stale comment in `backend/app/api/rate_limit.py` that still attributed the rewrite to uvicorn.
- **B-NEW-4** — L4's deletion of `test_create_job_endpoint_has_rate_limit_attached` left `POST /api/jobs` rate-limit wiring untested. The deletion note claimed coverage was inherited from `test_providers_models_rate_limit_blocks_after_threshold`, but that test exercises a different endpoint (`@limiter.limit("10/minute")` on `/api/providers/models`) — slowapi counters are per-route, so removing the `@limiter.limit("20/minute")` decorator from `/api/jobs` would not have broken any test. Added `test_create_job_endpoint_is_rate_limited` which sends 21 POSTs with an invalid file extension (cheap 400 from the route body) and asserts the 21st returns 429. The `client` fixture now also resets the slowapi limiter on teardown so this test doesn't poison `test_integration`'s apps with an exhausted counter.
- **T-FLAKY-1** — The two `LoggingObserver` level-mapping tests added in L8 asserted strict counts (`len(records) == 3` / `== 6`) against the unfiltered `caplog.records`. A future change adding a same-level log call in any other module would flip the count and trip the assertion despite the contract under test (the `_WARNING_EVENTS` mapping) being intact. Replaced with a `r.name == "alto_core.pipeline"` filter + per-event-type substring check on the formatted message.
- **S1** — Pinned the `_remove_job` locking contract documented in L6 with a new test (`test_remove_job_is_invoked_under_lock_during_eviction`) that spies on `_remove_job` and records `store._lock._is_owned()` at each call. A future refactor adding a new caller outside the lock would trip this test rather than introduce a subtle race in the three dict mutations + filesystem cleanup that `_remove_job` performs. Private-API call kept inside test code; production stays clean.
- **S6** — `backend/app/alto/_norm.py` and `backend/app/alto/_ns.py` had zero external consumers (verified with repo-wide grep). Both are private modules by name (leading underscore), so removal is non-breaking. The `app.alto.__init__` docstring now points future callers at `alto_core.alto._norm` / `alto_core.alto._ns` directly.
- **S7** — `alto-core-tests` and `alto-core-build` now run under a matrix of Python 3.11/3.12/3.13, matching the package's `requires-python` and Trove classifiers. Pre-S7 the 3.12 and 3.13 classifiers in `pyproject.toml` were unverified promises; now CI enforces them. `fail-fast: false` so one broken version doesn't mask others. Linting, types, security, backend and frontend stay on 3.11 — they're not the public API surface.

### Tests count evolution (post-L8)

- Avant la vague corrective : 343 backend + 6 alto-core + 12 frontend = 361 total.
- Après la vague (8 commits) : 350 backend + 6 alto-core + 12 frontend = 368 total (+7 backend).
- Breakdown des +7 :
  - +1 `test_create_job_resolves_timeout_seconds_dynamically` (B-NEW-1).
  - +4 dans `test_release_tooling.py` (B-NEW-2, paramétrés par fichier de tooling).
  - +1 `test_create_job_endpoint_is_rate_limited` (B-NEW-4).
  - +1 `test_remove_job_is_invoked_under_lock_during_eviction` (S1).
  - 0 net pour T-FLAKY-1 (2 tests réécrits, pas ajoutés).
  - 0 net pour S6 (suppression de fichiers, pas de tests).
  - 0 net pour S7 (CI seulement, pas de tests).

### Files removed (post-L8)

- `backend/app/alto/_norm.py` (S6).
- `backend/app/alto/_ns.py` (S6).
