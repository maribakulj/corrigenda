# ADR-005 — `CorrectionPipeline`: one run per instance; the manifest is consumed

Status: superseded by ADR-011 (slice E, 2026-07) — kept for history

## Context
Historically, per-run state (counters, producer ops, accepted snapshots,
finalisation owners) lived on the pipeline instance and was reset at the
start of `run()`; two concurrent `run()` calls on one instance silently
contaminated each other. `run()` also mutates the input manifest.

## Decision (as accepted, 2026-07)
1. **Per-run state lives in a `RunContext`** created fresh at the top of
   every `run()` and threaded through the internal methods. The pipeline
   instance carries only immutable configuration (policies, provenance
   labels) and injected dependencies. State contamination between runs
   is now structurally impossible, not prevented by manual resets.
2. **The one-run-at-a-time guard stays.** The injected `observer` and
   `output_writer` are shared instance dependencies: two concurrent runs
   would interleave their events and overwrite each other's outputs
   (`write_trace` has no run discriminator). A concurrent call raises
   `RuntimeError` immediately (guard released on any exit, so sequential
   re-use works). Concurrent callers build one pipeline per run.
3. **`run()` keeps mutating the input manifest** — explicitly documented
   as CONSUMED. Returning a copy was evaluated and rejected: manifests of
   large corpora are the dominant memory cost, and the library's own
   callers (backend runner, harness) all re-parse per run anyway.

## Superseded how (ADR-011 slice E)
Both remaining rationales dissolved: slice D-fin removed the
`output_writer` (the engine never persists), and slice E ended input
mutation — `run()` works on its own deep copy and returns the decisions
on `CorrectionResult.decisions`. With per-run state fully contained
(RunContext + private copy), the guard was removed: one instance
supports concurrent runs, the input document is never consumed, and
re-running the same document always starts from the original OCR text.
Point 1 (RunContext) survives unchanged inside ADR-011's design; the
memory trade-off of point 3 was re-decided in ADR-011 — a per-run copy
is the price of a side-effect-free engine.
