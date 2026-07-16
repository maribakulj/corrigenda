# ADR-009 — LineRef: document-wide line lookups carry a qualified identity

Status: accepted (2026-07)

## Context
Line identity is `(page_id, line_id)` everywhere in the product (README,
frontend `lineKey.ts`, API read models) — yet the engine's own
document-wide state was keyed three different ways: hand-built composite
strings (`f"{page_id}:{line_order_global}:{line_id}"` for traces,
pre-revert snapshots and finalization owners), raw `(page_id, line_id)`
tuples (producer-op capture, cross-page hyphen indexes), and bare
`line_id` strings for page-scoped maps. Every hand-built key is a
collision waiting for the string that contains the separator, and every
tuple is an unlabeled convention the type system cannot check. The
frontend shipped this exact bug class (V1.1: two files both containing
`L1` overwrote each other's traces).

## Decision
1. One key type: `corrigenda.core.identity.LineRef` — a frozen,
   slotted dataclass `(page_id, line_id)`. Every document-wide mutable
   map in the engine (traces, `accepted_snapshot`, `finalized_owner`,
   `producer_ops`, cross-page hyphen indexes, `CorrectionResult.traces`)
   is keyed by it; `line_ref(lm)` derives it from a manifest line.
2. The pair is fully qualifying because ADR-007 makes `page_id`
   document-unique (parsers disambiguate cross-file collisions; the
   pipeline door refuses duplicates). The source file is deliberately
   NOT in the key: it is a property of the page
   (`PageManifest.source_file`), and a key carrying it could name one
   physical line two ways. The planned immutable `SourceDocument`
   (plan P3.4) will hold the source dimension.
3. Bare `line_id` strings remain legal ONLY for lookups already scoped
   to one page or one source file (chunk plans, rewriter maps,
   `EditScript` ops — whose refs are qualified separately by the
   edit-protocol work).

## Consequences
A cross-page or cross-file keying mistake is now a type error, not a
runtime overwrite. `CorrectionResult.traces` changed key type (string →
`LineRef`) — a breaking change shipped in 0.9.x with no known consumer
(the backend reads `result.report`). `line_order_global` no longer
participates in trace keys; it was redundant under ADR-007's uniqueness
guarantees.
