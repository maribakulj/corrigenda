# ADR-006 — The pipeline emits events; it never logs

Status: accepted (2026-07)

## Context
The library runs inside hosts with very different observability stacks
(FastAPI backend with JSON logging, notebooks, batch scripts). A
`logging` call inside the pipeline forces every host to adopt the
library's logger names, levels and formatting, and couples the pure
core to a side-effecting global.

## Decision
`CorrectionPipeline` reports everything observable through the injected
`PipelineObserver` (`on_event(type, payload)`): retries, fallbacks,
chunk errors, reconcile stats, rewriter stats. The host decides what to
log, trace or stream. The library's core never imports `logging`.

Observer events are emitted ON the event loop, never from worker
threads — host-side observers (SSE queues) are not thread-safe.

## Consequences
Diagnosing a run means subscribing to events, not scraping logs.
Hosts that want logs write a five-line observer. Payload keys are part
of the public event contract documented in the schemas module.
