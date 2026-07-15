# ADR-005 — `CorrectionPipeline`: one run per instance; the manifest is consumed

Status: accepted (2026-07) — updated after the RunContext extraction (V4.1-L)

## Context
Historically, per-run state (counters, producer ops, accepted snapshots,
finalisation owners) lived on the pipeline instance and was reset at the
start of `run()`; two concurrent `run()` calls on one instance silently
contaminated each other. `run()` also mutates the input manifest.

## Decision
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

## Consequences
Servers create one pipeline per run (the backend already does).
Consumers re-running on the same manifest start from the previous
run's corrected state — pass a fresh parse if that is not wanted.
Sequential runs on one instance share no state (pinned by
`test_sequential_runs_share_no_state`).
