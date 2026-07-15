# ADR-005 — `CorrectionPipeline`: one run per instance; the manifest is consumed

Status: accepted (2026-07) — partially superseded when the RunContext
extraction lands (planned before the SemVer freeze)

## Context
Per-run state (counters, producer ops, accepted snapshots, finalisation
owners) lives on the pipeline instance and is reset at the start of
`run()`. Two concurrent `run()` calls on one instance silently
contaminate each other; `run()` also mutates the input manifest.

## Decision
The contract is documented on `run()` and ENFORCED: a concurrent call
raises `RuntimeError` immediately (guard released on any exit, so
sequential re-use works). The long-term fix — a per-run `RunContext`
holding all execution state, leaving the pipeline an immutable
configuration — is scheduled before the 1.0 SemVer freeze.

## Consequences
Servers create one pipeline per run (the backend already does).
Consumers re-running on the same manifest start from the previous
run's corrected state — pass a fresh parse if that is not wanted.
