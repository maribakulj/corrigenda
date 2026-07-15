# ADR-001 — Line identity is `(page_id, line_id)` everywhere

Status: accepted (2026-07)

## Context
ALTO `TextLine@ID` is an XML NCName scoped to its own document: two
uploaded files routinely both contain `L1`, `L2`, … Any structure keyed
on `line_id` alone silently collapses homonymous lines (last-write-wins
traces, cross-page selection bleed). The library had already adopted
the composite key; the frontend regressed independently.

## Decision
Every layer — library ops/traces, API read models, frontend state
(`frontend/src/lib/lineKey.ts`, key `page_id:line_id`; NCNames cannot
contain `:`) — identifies a line by `(page_id, line_id)`. `page_id`
uniqueness across the whole document is enforced at the pipeline door.

## Consequences
Selection/trace APIs carry both identifiers. Tests must include two
files with deliberately identical TextLine IDs.
