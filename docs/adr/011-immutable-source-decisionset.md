# ADR-011 — Immutable source, DecisionSet, side-effect-free engine

Status: accepted (2026-07) — landing in slices; see Staging.

## Context
`run()` mutates its input manifest (corrected_text/status/hyphen
pointers), which is why one pipeline instance allows one run at a time
(ADR-005's `_running` guard), why the run-independence property must
test two PARSES instead of two runs over the same document, and why
every downstream pass re-derives state the mutation invalidated
(ADR-010's design constraint). The core also owns concerns that belong
to its callers: an injected `OutputWriter` plus an `apply=` flag decide
persistence inside the engine, and the P1.4 projection invariant
re-parsed the just-serialized output XML because the rewrite returned
bytes only — a second full lxml parse per file to learn what the
rewriter already knew.

## Decision
1. Source documents become immutable (`SourceDocument`/`SourcePage`/
   `SourceLine`); the engine produces `ProposalSet` → `DecisionSet`
   (immutable) → `CorrectionResult` whose artefacts (per-file XML,
   report, EditScript, metrics) are computed values. `run()` never
   modifies its input.
2. Persistence leaves the core: the engine returns bytes;
   `result.write(dir)` is a helper outside the engine; the file
   transaction stays with the server (`backend/app/jobs/runner.py`),
   which already owns it.
3. The rewrite returns a `RewriteResult(xml_bytes, metrics,
   rewriter_paths, texts, losses)` — one value per file. The texts are
   read off the very tree the bytes were serialized from, so the
   projection invariant verifies without a second parse (serialization
   fidelity is lxml's contract; the invariant guards the rewriter's
   tree diverging from the run's decisions), and the format's
   granularity-loss counters finally reach
   `CorrectionReport.format_losses`.
4. `_running` goes away (a frozen engine is reentrant), and the
   manifest's redundant counters (`total_pages/blocks/lines`) become
   computed properties.

## Staging
- **Slice A (landed)**: `RewriteResult` — the format seam returns one
  value; the pipeline's second parse of the output is gone;
  `format_losses` is wired (it existed on the report schema and was
  never populated by a run). `RewriteResult.__iter__` yields the
  historical `(xml_bytes, metrics, rewriter_paths)` triple so existing
  tuple call sites survive the migration — attribute access is the
  contract; the shim goes when the tuple sites do.
- **Slice B (landed)**: `DocumentManifest.total_pages/blocks/lines`
  are computed fields (still serialized); the lying-totals validator is
  retired — a derived count cannot contradict the content, and legacy
  constructor kwargs are ignored rather than trusted.
- **Slice C (landed)**: `corrigenda.core.decisions` defines
  `LineDecision`/`DecisionSet`, materialized once after the global
  consistency pass; the terminality backstop became the set's
  construction invariant (a PENDING line refuses materialization), and
  the projection invariant, the result's fallback accounting, the
  final-EditScript builder AND the report builder (P3.5's `LineOutcome`
  restructure, `build_line_outcomes`) read the DecisionSet instead of
  re-walking the manifests. The pointer fields' retirement still folds
  in ADR-010's `BOTH`-as-derived-detail.
- **Slice D (landed)**: `CorrectionResult.corrected_files` carries
  every corrected XML (the result IS the output) and
  `result.write(dir)` persists artefacts + report caller-side. D-fin:
  `OutputWriter` and `apply=` left the engine surface — the engine
  never persists; the backend's JobRunner stages the result's bytes
  through ITS OWN writer port (`app.protocols.OutputWriter`, moved
  out of the library) and keeps the commit/discard transaction; the
  quickstart and docs migrated to `result.write(dir)`. ADR-005's guard
  survives on its remaining rationale (shared observer + manifest
  mutation) until slice E.
- **Slice E (landed)**: mutation ends at the engine boundary — `run()`
  works on its own deep copy of the input; the caller's document is
  never written, and the run's outcome is `result.decisions` (the
  `DecisionSet`, now exported with `LineDecision`/`LineRef`). The
  `_running` guard is removed (ADR-005 superseded): concurrent runs on
  one instance work, and the P0 run-independence property is now "two
  runs on the SAME document object". The backend projects
  `result.decisions` onto ITS stored manifest for its read models
  (/diff, /layout, lines_modified) — server-owned state, server-owned
  mutation. Remaining tail (folds into P3.5's model restructure):
  freezing the manifest TYPES themselves (`Source*` renames) and
  retiring the per-line pointer/SUBS fields the working copy still
  uses internally — behaviourally invisible either way, since no
  caller can observe the working copy.

## Consequences
The engine is now a function of its input: same document, same
decisions, no side effects — the input can be reused, cached, or run
concurrently. The cost, re-decided from ADR-005's point 3, is one deep
copy per run. Each slice kept the whole suite (and the
chunking-invariance gates) green on its own.
