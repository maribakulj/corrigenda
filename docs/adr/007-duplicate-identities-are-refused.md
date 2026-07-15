# ADR-007 — Duplicate identities are refused, never disambiguated

Status: accepted (2026-07)

## Context
Every internal association between a correction and its physical line —
rewriter lookup, trace projection, hyphen partner resolution — is keyed
by `line_id` within one source file (and `(page_id, line_id)` across
files, see ADR-001). A document where two `TextLine` elements share an
ID is *ambiguous*: any silent strategy (first wins, last wins, suffixing)
risks applying a correction to the wrong physical line of a heritage
document.

## Decision
Ambiguous identities are refused explicitly with `DuplicateIdError`
(a `ParseError`), at two doors:

1. **Parse time** — each format parser validates per-file uniqueness of
   page/block/line IDs.
2. **Pipeline entry** — `run()` re-validates the manifest (hand-built
   manifests get the same guarantee as parser-built ones), including
   document-wide `page_id` uniqueness.

Cross-FILE `page_id` collisions are the one legitimate case (two
uploads of similar files): they are deterministically qualified at
build time, before hyphen linking, so every downstream reference is
already unambiguous.

## Consequences
A malformed document fails fast with an actionable message instead of
producing a plausibly-wrong corrected file. Consumers embedding the
library must fix source IDs rather than expect the library to guess.
