# Changelog

All notable changes to **corrigenda** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **P1-1 — recursive structure traversal.** Both parsers only visited
  *direct* children: ALTO ``TextBlock``s nested inside a ``ComposedBlock``
  and PAGE ``TextRegion``s nested inside another region were silently
  dropped — their lines never entered the manifest and were never
  corrected. Both parsers now walk the whole subtree in document order
  (each PAGE region still contributes only its direct lines, so nothing
  is double-counted). ALTO's container rule is unchanged (``PrintSpace``
  when present, else the whole ``Page``).

### Changed

- **P2-5 — configuration models validate invariants, not just types.**
  Every policy knob used to be a bare `int`/`float`: negative backoffs,
  zero chunk limits, out-of-range similarity ratios, temperatures outside
  [0, 2], a window overlap ≥ the window size, `target_line_ids` outside
  the chunk's `line_ids` and contradictory `DocumentManifest` totals were
  silently accepted, then produced arithmetic nonsense deep inside the
  pipeline. All config models (`ChunkPlannerConfig`, `GuardConfig`,
  `RetryPolicy`, `PairingPolicy`), `ChunkRequest` and `DocumentManifest`
  now fail fast at construction (`Field(ge/gt/le)` + cross-field
  validators). Policy fingerprints are unchanged (values didn't move).
  Deliberate exception, documented: *data* models fed from wild heritage
  XML (`Coords`, …) stay tolerant per F5 — a skewed scan's slightly
  negative position must not abort the file; geometry consumers treat
  degenerate boxes defensively instead.
- **P2-8 — `MatchAnchor.occurrence` is now `int | None = None`.** The old
  `int = 0` default conflated "producer said nothing" with "producer wants
  the first occurrence", making the first of a repeated pattern
  *inexpressible* (0 + multiple matches → rejected as ambiguous). `None`
  (the new default) requires uniqueness — same behaviour as before for
  producers that never set the field — while an explicit integer,
  **including 0**, always selects that occurrence. Aligns the
  implementation with §4.3's own wording ("plusieurs occurrences sans
  `occurrence` explicite → rejetée").
- **P2-9 — the E4 line budget counts characters actually changed.**
  `edit_line_max_changed_chars` used to sum `abs(len(replacement) −
  len(span))`: a length-neutral rewrite of 100 characters cost 0, so the
  knob bounded length drift, not the amount of text changed. Each span op
  is now costed by the size of its differing window after trimming the
  common prefix/suffix (0 for identical text, its length for a pure
  insertion, the larger side for a full rewrite — an upper bound on the
  Levenshtein distance). Length-neutral rewrites that previously slid
  under the budget are now rejected with `e4_line_budget`.
- **P1-2 — the default `PairingPolicy` is now geometric.** The historical
  default accepted *every* sequential hyphen-pair candidate — on layouts
  whose serialisation order diverges from reading order, a PART1 line
  could silently pair with a marginal note, an unrelated block, or an
  out-of-order line, shaping the LLM context with the wrong partner.
  Heuristic (trailing-dash) pairs are now vetted at pairing time: same
  block → candidate below within ``max_gap_line_heights`` (default 3.0)
  of the line's own height; cross-block same page → either a downward
  continuation with horizontal overlap (next block, same column) or an
  upward, horizontally disjoint, entirely-above jump (top of the next
  column — direction-agnostic, RTL-safe). Engine-asserted (explicit
  ``SUBS_TYPE``/``HYP``) pairs, cross-page seams and degenerate
  (coordinate-less) geometry are always trusted. New fingerprinted
  fields ``geometric_checks`` / ``max_gap_line_heights`` /
  ``max_rise_line_heights``; ``PairingPolicy(geometric_checks=False)``
  restores the historical behaviour exactly. Composite config
  fingerprint moves ``3a06d0a93ac4eedc`` → ``216aa712f1e99b79``.

### Added (provider error taxonomy — P0-1/P0-2)

- **`ProviderPermanentError`** *(in `corrigenda.core.protocols`, next to
  `ProviderTransientError`)* — the provider definitively rejected the
  request (invalid credentials, unknown model — the 4xx-non-429 family).
  The pipeline treats it as **fatal for the whole run**: never retried,
  never downgraded, never converted into an OCR fallback; it propagates
  out of `run()` before any output is written, like `CorrectionAborted`.
  Providers that don't wrap keep the old degrade-to-fallback behaviour.
- **P0-2 — the per-chunk `except Exception` is gone.** Only recoverable
  domain errors (`CorrectionError` subclasses) may be absorbed as a
  `chunk_error` event + continue; a programming error (KeyError, broken
  invariant, pydantic bug) now fails the run instead of letting it
  complete "successfully" with lines in an unknown state.

### Fixed (adversarial-review wave over the remediation itself)

- **Planner window walk survives config-validation bypass.** Pydantic's
  `model_copy(update=…)` bypasses the P2-5 validators, so
  `line_window_overlap >= line_window_size` spun the window loop forever
  (reproduced). A progress clamp restores the historical guarantee.
- **LINE-mode chain cap now UNLINKS the cut pair.** Truncating a
  longer-than-cap hyphen chain used to leave the pair straddling the cut
  still linked across two chunks — the validator skips such pairs and the
  reconciler could write across the boundary. Both sides now degrade to
  independent lines (OCR text preserved verbatim), so pair atomicity
  stays true by construction.
- **ALTO IDNEXT:** an empty-string block ID crashed the chain walk with a
  raw `KeyError`; an IDNEXT pointing outside the page (cross-page article
  continuation — a legitimate METS/ALTO pattern — or a margin block) now
  ends the chain instead of voiding the page's whole declared order.
- **ALTO margins:** without `PrintSpace` the recursive block walk swept
  margin-nested blocks (running heads, page numbers) into correction
  scope; they are explicitly excluded again in both container shapes.
- **Duplicate-ID gate covers the whole tree.** The rewriters match
  TextLine ids document-wide, but the parse gate only checked manifest
  scope: a margin line reusing a body line's ID passed upload validation
  and exploded at rewrite time, after the full producer spend. Both
  parsers now scan every TextLine id in the file.
- **Block IDs are page-scoped.** Per-page OCR exports that reuse
  `block_0`/`block_1` on every page of a file are legitimate (every block
  lookup downstream is page-scoped) — the per-file check refused them.
- **PAGE ReadingOrder: partial declarations are ignored.** A declaration
  covering only some regions used to yank the referenced regions ahead of
  everything else, reordering text it said nothing about; only a
  declaration covering every id-bearing region now reorders (same
  conservative rule as the IDNEXT fallbacks).
- **Identical line boxes = synthetic geometry.** Exports that copy the
  block's coords onto every line no longer have their heuristic hyphen
  pairing silently disabled by the P1-2 geometric vetting.
- **Duplicate reverts are pair-atomic and cover page seams.** Reverting
  one member of a reconciled hyphen pair left a mixed OCR+corrected pair
  (the state `reconcile_hyphen_pair` forbids) — the revert now extends to
  the partner (`adjacent_duplicate_pair_atomicity`), the revert logic is
  one shared helper instead of two divergent copies, the P2-6 pass is
  restricted to actual chunk-boundary pairs (no redundant re-checking),
  and page-boundary seams are checked too (the same leak one level up).
- The explicit-pair bypass in `PairingPolicy` is documented precisely:
  the opt-in legacy vetoes (`same_block_only`, `max_vertical_gap`) still
  apply to explicit pairs; only the geometric vetting is bypassed.
- `docs/edit-protocol.md` updated to the new `occurrence` semantics.

### Fixed (guards & budgets)

- **P1-8 — `max_input_chars_per_request` is now a real bound.** Only PAGE
  and BLOCK honoured the char budget; a WINDOW of pathologically long
  lines blew straight past it and LINE mode could follow an unbounded
  hyphen chain. Windows are now bounded by BOTH the line count and the
  char budget (the overlap step follows the actual window end so a
  budget-shortened window never skips lines — full windows keep the
  historical fixed step exactly), and LINE chains are capped at
  `max_lines_per_request`. Two documented atomic exceptions may
  overshoot: a hyphen chain (splitting corrupts reconciliation) and a
  single line longer than the whole budget. The budget's semantics are
  now documented precisely: it counts RAW OCR text, not the enriched
  request envelope — size it with headroom.
- **P2-6 — duplications straddling a chunk boundary are now caught.**
  Adjacent-duplicate detection ran per chunk on that chunk's target
  lines only, so two document-adjacent lines owned by different chunks
  were never compared. A page-level pass after all chunks re-checks
  every adjacent pair in reading order (idempotent over the intra-chunk
  results) and reverts both sides of a boundary duplicate to OCR with
  `adjacent_duplicate_detected`.
- **P2-7 — guards stage-strictness doc contradiction resolved.**
  `guards.py` called Stage A "the strictest" while the config documents
  Stage A as more permissive on PART1 growth (2 words vs 1 at Stage B).
  The docs now say what the code does: Stage A carries the most
  aggressive *remedy* (whole-chunk retry), Stage B the strictest
  *thresholds* — a maintainer can no longer tune them backwards on the
  strength of the old sentence.

### Added

- **P1-1 — explicit reading order.** PAGE ``ReadingOrder`` declarations
  (nested Ordered/Unordered groups, ``RegionRefIndexed`` by ``@index``)
  and ALTO ``IDNEXT`` block chains now drive block/region order — hence
  ``line_order_global``, prev/next neighbour context and hyphen pairing —
  instead of raw XML serialisation order (wrong on multicolumn layouts
  whose declaration diverges). Conservative by construction: regions not
  covered by the declaration follow in document order; an inconsistent
  declaration (dangling ref, cycle, converging IDNEXT chains) falls back
  to document order entirely — the library never guesses. Corpus files
  whose declaration matches document order (all of ``examples/``) produce
  byte-identical output.

- **`DuplicateIdError`** *(top-level, subclasses `ParseError`)* — P0-5
  identity-uniqueness invariant. A source file whose Page / TextBlock /
  TextLine IDs are not unique is now refused explicitly instead of being
  silently mis-corrected: previously, two `TextLine` elements sharing an ID
  made the rewriters apply the *last* parsed manifest to **both** physical
  lines (last-write-wins on an internal `line_id` dict), destroying one
  line's text. Enforced in four layers: both format parsers (right after
  manifest construction), `CorrectionPipeline.run()` (at the door, so
  hand-built manifests get the same guarantee — including cross-file
  `page_id` collisions), both rewriters, and both `extract_output_texts`.
  Duplicate IDs across *different* source files remain legitimate (every
  downstream lookup is scoped to one file). Additive change: existing
  `except ParseError` / `except CorrectionError` call sites keep working.

## [1.0.0] — 2026-07-06

First public release. Everything below shipped together as **the** 1.0 of
`corrigenda` — nothing was ever published under an earlier name or number,
so there is no deprecation layer anywhere: final import paths and final
schemas from day one. The public surface is pinned by an executable
snapshot test (`tests/test_public_api_snapshot.py`) and governed by strict
SemVer from here on (see `docs/versioning.md`).

Highlights: ALTO **and PAGE XML** backends producing one common
`DocumentManifest`; the §4 span edit protocol (`EditScript`,
`ReplaceLine`/`ReplaceSpan`, `MatchAnchor`→`RangeAnchor`); producers as
first-class citizens (`EditProducer`, deterministic `RulesProducer`, LLM
adapter, vision envelope with zero pixel I/O); the versioned
`CorrectionReport` as the single trace artefact; four frozen, fingerprinted
policies; byte-parity golden gates over a real BnF/Transkribus corpus.

### v1.0 normative corrections (SPECS_LIB_V2 §7)

- **F3** — the parser tolerates comments / processing-instructions among a
  `TextLine`'s children (they carry a callable `tag`); a trailing comment
  no longer aborts the whole file.
- **F5** — `_int_attr` parses float-valued coordinates (`"123.0"`, `"800.9"`)
  via `int(float(...))`, truncating toward zero. Non-numeric values still
  raise.
- **F6** *(byte change)* — slow-path token geometry: the 0.6 space weight now
  enters the total weight and rounding is spread by cumulative rounding.
  Widths still sum exactly to the line width; the final token only absorbs
  residual rounding instead of every space's accumulated deficit. Changes
  output bytes on slow-path lines with interior spaces (UNTOUCHED /
  SUBS_ONLY / FAST paths unaffected).
- **F13** — `GuardConfig` (frozen, injectable) gathers every anti-migration /
  acceptance threshold from the three guard stages; defaults reproduce the
  historical constants byte-for-byte. `FrozenPolicy.policy_fingerprint()`
  gives a stable hash for provenance (§11). Threaded through `check_line`,
  `check_adjacent_duplicates`, `reconcile_hyphen_pair`,
  `validate_llm_response`, and `CorrectionPipeline(guard_config=…)`.
- **F7** — `PairingPolicy` (frozen, injectable) makes hyphen pairing a seam;
  default reproduces the historical purely-sequential pairing. Forwarded
  through `build_document_manifest` / `parse_alto_file`.
- **F2** *(byte change)* — a changed `CONTENT` drops the now-stale `WC`/`CC`
  confidences (fast path, per changed String); the slow-path rebuild
  recycles only `ID` and `STYLEREFS` (§6.1 whitelist), inherits `VPOS`/
  `HEIGHT` from the line, recomputes `HPOS`/`WIDTH`, and never carries
  `WC`/`CC`/`SUBS_*`. Changes output bytes on slow-path lines and on
  fast-path Strings whose CONTENT changed.
- **F4** — the UNTOUCHED comparison strips both sides, matching the parser's
  `ocr_text` derivation; a line with a trailing `<SP/>` under identity
  correction now takes the UNTOUCHED path instead of being rewritten.
- **F9** — `RetryPolicy` (frozen, injectable) externalises the temperature
  ramp, attempt cap, backoff bases and per-chunk budget.
  `RetryPolicy.default()` reproduces the historical ramp (0.0/0.3/0.5, cap 3)
  to the byte; `RetryPolicy.deterministic()` sets every temperature to 0.
  `CorrectionPipeline(retry_policy=…)`.
- **F10** — `CorrectionPipeline.run(should_abort=…)` cooperative cancellation,
  probed between pages and chunks; raises `CorrectionAborted` (new
  `corrigenda.errors` module, `CorrectionError` root) before any output is
  written. In-flight provider calls are not interrupted.
- **F1** *(behaviour change on failure paths)* — a chunk whose retry budget is
  exhausted is re-planned one granularity finer (PAGE→BLOCK→WINDOW→LINE) and
  retried (`chunk_downgraded` event), bounded by `RetryPolicy.per_chunk_budget`
  (default 6). Only lines whose finest-grain chunk still fails fall back to OCR;
  a transient burst now recovers instead of reverting the whole chunk. New
  `chunk_downgraded` event added to the SSE contract.
- **F8** — overlapping windows distinguish *target* vs *context* lines
  (`ChunkRequest.target_line_ids`): each line is corrected in exactly one
  window (its best-following-context window), hyphen pairs kept together;
  overlaps become pure context. No effect on PAGE-granularity documents.
- **F14** *(pre-1.0 break)* — `BaseProvider.complete_structured` returns
  `(dict, Usage | None)`. New `Usage` model; `CorrectionResult.usage`
  aggregates the run; per-chunk tokens on the `chunk_completed` event.
- **Error hierarchy (§8.4)** — `corrigenda.errors`: `CorrectionError` root with
  `ParseError`, `ValidationError` (both also `ValueError`), `CorrectionAborted`;
  `HyphenIntegrityError` is now a `ValidationError`. `validate_llm_response`
  raises `ValidationError`.
- **CorrectionReport + dry-run (§9)** — the per-line trace is promoted to a
  public, versioned `CorrectionReport` (`report_version` "1.0"), returned on
  `CorrectionResult.report`. `run(apply=False)` runs the full pipeline
  (production, guards, reconciliation, in-memory rewrite) but never calls the
  `OutputWriter` — the report is the deliverable.
- **Provenance (§11)** — the corrected XML's `processingStep` now records the
  library version and a configuration fingerprint
  (`RetryPolicy`+`GuardConfig`+`ChunkPlannerConfig`) alongside provider/model.
- **py.typed + `mypy --strict` (F12/§8.3)** — PEP 561 marker shipped in the
  wheel; the package passes `mypy --strict` (new `corrigenda-types` CI job).
- **F12 (relocation)** — `Provider`, `JobStatus`, `JobManifest` (and its
  `images` map) moved to the backend (`app.schemas.job`); the vestigial
  `status` field was dropped from `PageManifest`/`DocumentManifest`. The core
  keeps only the domain enums (`LineStatus`, `ChunkGranularity`, `HyphenRole`,
  `PipelineEventType`). Top-level public surface is now 34 symbols.
- **F11** — the algorithm tests were repatriated into
  `packages/corrigenda/tests`; the package gates its own coverage (~86%, gate
  85%) and its CI job runs pytest with `--cov=corrigenda`.

### Span edit protocol (SPECS_LIB_V2 §4 / §5)

- `corrigenda.core.editing` — `EditScript` of `ReplaceLine` / `ReplaceSpan`
  ops (no structural op ⇒ invariant I2 by type). `RangeAnchor` (offsets)
  and `MatchAnchor` (exact substring) normalise to a single `RangeAnchor`
  against the canonical text; unfound / out-of-range / ambiguous anchors
  reject the op (I2 fallback). `apply_edit_script` enforces E1–E5 (E6 stays
  the downstream three-stage matrix). **E4/E5 gate `replace_span` only** —
  `replace_line` keeps E1/E3/conflict, so re-expressing today's whole-line
  response is byte-identical (proved on sample.xml / X0000002.xml).
- `corrigenda.producers.rules` — deterministic `RulesProducer` (§5.3):
  literal/regex substitutions with an optional lexicon guard, emitting
  `replace_span` + exact `RangeAnchor`. Zero deps, byte-reproducible; the
  first real span emitter and a free pre-LLM pass. `default_french_ocr_
  rules()` ships ſ→s and ﬁ/ﬂ ligatures.
- `EditProducer` contract (§5.1) with `wants_geometry` / `wants_image`;
  `LLMEditProducer` adapts a `BaseProvider` (emits `replace_line` + Usage).
  Vision envelope (§4.1): `LineGeometry` + opaque `ImageRef` copied by the
  compiler only on request — the library opens no pixel (**I4**, enforced
  by an AST contract test). `require_source_images` raises `ValidationError`
  for a `wants_image` producer run without images.
- Pipeline: producers return `EditScript`s that are normalised and applied
  through `apply_edit_script` (byte-parity via the golden gate);
  `CorrectionResult.edit_script` surfaces the normalized script, and a dry
  run (`apply=False`) returns it as the deliverable.
- **BREAKING — §5.1 resorption.** `CorrectionPipeline` is constructed
  around an `EditProducer`; `run()`/`run_sync()` no longer take
  `api_key`/`model`/`provider_name` (credentials live inside the producer;
  the provenance labels are constructor state). `run(source_images=…)`
  forwards opaque image refs, checked at start-up for `wants_image`
  producers. `CorrectionPipeline.for_provider(provider, api_key=…,
  model=…, provider_name=…)` is the one-call migration for the LLM case.
  The pipeline still drives the retry ramp (it hands each attempt a policy
  whose first temperature is that attempt's — hyphen 0.0 pin included), so
  retry classification, temperatures and output bytes are unchanged. A
  producer may declare `requires_full_coverage = False` (rules engine: no
  op == no edit); LLM producers keep strict 1:1 coverage → retry. The
  prompt/schema seam moved into `LLMEditProducer`; the import-contract's
  pinned core exceptions are now `_default_format_adapter` + `for_provider`.
- **BREAKING — JobTrace → CorrectionReport unification (§9).** `JobTrace`
  is deleted; `trace.json` and the backend's `/trace` endpoint carry the
  versioned `CorrectionReport` verbatim (`report_version`, `run_id` ==
  job id, `total_lines`, `lines`). Backend `JobManifest` gains `report`;
  the frontend `TraceData` type mirrors the report.

### PAGE XML support (SPECS_LIB_V2 §6.2 / §6.3, P1–P7)

- New `formats/page/` backend (parser, rewriter, adapter) producing the
  **same `DocumentManifest`** as ALTO — the pure core is reused unchanged.
- **P1** — geometry is polygons. `Coords@points` is kept verbatim on the
  new `Coords.polygon` field; the enclosing bbox is derived for the
  planner. Geometry is **never rewritten** (no geometric slow path).
- **P2/P3** — canonical line text = the minimal-`@index` line `TextEquiv`
  (absent index ≡ 0), else the space-joined `Word` Unicode; NFC + strip.
  On rewrite the canonical `TextEquiv` is updated (Unicode + `PlainText`),
  its stale `@conf` dropped and alternative `TextEquiv` removed.
- **P4** — words: fast path (count unchanged) updates each `Word`'s
  `TextEquiv` in place and keeps its `Coords`; slow path (count changed)
  drops the `Word` children (text lives at line level) and counts the lost
  granularity.
- **P5** — heuristic-only hyphenation over `- ¬ ⸗ U+00AD` with chained
  `BOTH` detection; the source hyphen character is preserved verbatim on
  rewrite (E5 extended — no `¬` → `-`). The core reconciler's PART1 check
  now accepts the whole repertoire (`-` retained ⇒ ALTO byte-parity intact).
- **P6** — `custom` microformat: structural groups (`readingOrder`,
  `structure`) preserved verbatim; offset-anchored groups (`textStyle`,
  tags with `offset`/`length`) dropped when the line text changes and
  counted.
- **P7** — `make_safe_parser` throughout (the grep contract already spans
  `formats/**`); provenance as a `MetadataItem` on 2019+ schemas, else
  appended to `Metadata/Comments`; no wall-clock timestamp ⇒ deterministic
  output.
- **Shared pairing** — the second-pass hyphen linker, page-id
  disambiguation and cross-page linking moved to the pure `core.pairing`
  (both formats call it; §6.3 parity holds by construction).
- **`CorrectionReport.format_losses`** — optional aggregate of
  format-specific granularity losses (`words_dropped`,
  `custom_offset_stripped`, …), fed by `PageRewriterMetrics.as_losses()`.
  Additive/optional ⇒ `report_version` stays `"1.0"`.
- Validated on the real corpus (OCR17plus triplets, NewsEye columnar
  press): LaFayette parses 13 lines byte-identical to its ALTO4 export;
  identity round-trip is text-stable; synthetic fixtures pin `@index`,
  `@conf`, alternatives, `PlainText`, `custom` offsets, the 2019 namespace
  and the ⸗ Fraktur hyphen.

### Renamed (§14 — pre-publication, no aliases)

- Distribution **alto-core → corrigenda**, import package **alto_core →
  corrigenda**. *Corrigenda* — the printed errata leaf bound into books —
  is literally what this library produces, carries the heritage domain,
  and survives the PAGE XML extension (v1.1) where "alto" would become a
  lie. Nothing was ever published under the old name, so there is no
  deprecation layer: final import paths from day one. The repository slug
  (URLs in project metadata) still reads alto-llm-corrector until the
  GitHub repository itself is renamed. The `processingStep` provenance
  brand written into corrected XML is now `corrigenda` (no effect on the
  byte-parity corpus: its files carry no `<Processing>` element).

### Post-audit corrective rounds (same release)

- **F1×F8 fixed** — the granularity descent re-plans a failed chunk's
  *target* lines only; context lines are no longer stolen from their own
  window and corrected at a finer grain.
- **F1×F10 fixed** — `should_abort` is probed inside the descent (before
  each sub-chunk) and `CorrectionAborted` is never converted into a
  `chunk_error` event.
- **F8 (spec letter)** — `validate_llm_response(target_line_ids=…)`: the
  1:1 count is enforced on targets; a missing context-line output is not an
  error. Per-entry structural checks stay strict; hyphen integrity runs
  over the target set. `None` keeps the historical exact-count contract.
- **`run_sync()` (§8.1)** — synchronous façade over `run()`; refuses a
  running event loop.
- **`ChunkPlannerConfig` frozen (§8.2)** — now a `FrozenPolicy` with
  `policy_fingerprint()`, like the other three policies.
- **Provenance fingerprint unified (§11)** — public
  `CorrectionPipeline.config_fingerprint()`, composed from the four
  policies' public `policy_fingerprint()` values (sorted-JSON sha256/16)
  and now covering `PairingPolicy` (provenance-only ctor param).
  Reproducible by consumers from the public API.
- **Slow-path SP geometry recomputed** *(byte change)* — SPs no longer
  recycle stale pre-correction HPOS/WIDTH; their geometry comes from the
  same `_compute_geometry` pass as the surrounding Strings (contiguous
  layout).
- **§6.1 whitelist extended with `STYLE`** — inline styling (bold/italics)
  is preserved on the slow path alongside `ID`/`STYLEREFS`. The spec names
  only the latter two, but its doctrine targets data *invalidated* by the
  text change — styling is not; dropping it destroyed real formatting on
  the non-regression corpus. Flagged for spec ratification.
- **F6 degenerate floor fixed** — the min-1 deficit is repaid across
  multiple donors; the exact-sum invariant survives every feasible width.
- **F7 cross-page gap** — `max_vertical_gap` is skipped for cross-page
  candidates (VPOS restarts per page).
- **F14 event semantics** — `chunk_completed` reports the chunk's total
  usage across all attempts, not just the final successful call.
- **Byte-parity gate (§13 DoD)** — `test_byte_parity_corpus.py` pins
  sha256 golden hashes of two deterministic scenarios over the corpus.
  Verified against the pre-v1.0 baseline (commit 8c4789c): identity
  corrections are BYTE-IDENTICAL; scripted corrections differ only on
  documented F2 (WC/CC) and F6/§6.1 (geometry) line classes.

### Changed
- **Retry policy on HTTP 4xx (other than 429) is now non-retryable.**
  The previous class-name allowlist (`exc.__class__.__name__ ==
  "HTTPStatusError"`) caused `401`, `403`, `404`, `422` to be retried
  3 times with exponential backoff — a waste, because client errors
  (bad API key, wrong model, schema rejection) don't heal on retry.
  The classifier now routes on `isinstance(exc,
  ProviderTransientError)`, and providers' HTTP wrapper deliberately
  leaves 4xx-non-429 errors un-wrapped, so they reach the classifier
  as non-retryable and the chunk falls back to OCR source on the
  first failure. Pinned by
  `test_pipeline_classifies_client_http_4xx_as_non_retryable`.
  `5xx`, `429`, and transport-level failures (timeout, network,
  protocol) retain the previous 3-attempt exponential-backoff
  behavior.
- Clarified in the `### Added` section of `[0.1.0a1]` which symbols
  are re-exported at the package root (`from corrigenda import …`)
  versus the ones that are sub-module-only. The technical contract
  is unchanged — every symbol previously listed remains importable
  from its canonical path. (roadmap L5 / B5)

### Added
- `ProviderTransientError.status_code: int | None` — when the
  underlying transport failure was an HTTP error, the originating
  status code is preserved on the wrapped exception so observers can
  route on 429 vs 503 vs 500 without parsing the message. `None` for
  transport-level failures (timeout, network, protocol). The full
  underlying exception remains reachable via `__cause__` for callers
  that need response headers or the request URL.

### Documentation
- Public Pydantic models (`LineManifest`, `DocumentManifest`,
  `BlockManifest`, `PageManifest`, `JobManifest`, `ChunkPlannerConfig`,
  `LLMLineInput`, `LLMLineOutput`, `ModelInfo`, `Coords`) and enums
  (`JobStatus`, `LineStatus`, `ChunkGranularity`, `Provider`,
  `HyphenRole`) now carry a one-line docstring. PyPI consumers get
  IDE help/intellisense out of the box. (roadmap L5 / A5)

### CI / Release
- Single source of truth for the smoke-import check:
  `packages/corrigenda/_smoke_imports.py` iterates `corrigenda.__all__`
  and is invoked by `.github/workflows/ci.yml`,
  `.github/workflows/publish-corrigenda.yml`, and
  `scripts/release-corrigenda.sh`. Drift between the three is now
  impossible. (roadmap L5 / B6)
- Added `Programming Language :: Python :: 3.13` classifier
  (`requires-python = ">=3.11"` already permitted 3.13). (roadmap L5 / P3)

## [0.1.0a1] — 2026-05-25 (internal milestone — never published)

The extraction milestone under the working name `alto-core`. Kept for the
historical record; **this version never reached any index**, and every
item below is folded into 1.0.0 above.

Initial alpha release.

### Added

> **Import paths.** Each section below documents the path the listed
> symbols live at. Most are sub-module imports, e.g.
> `from corrigenda.formats.alto.rewriter import RewriterMetrics`. The shorter
> set of names re-exported at the package root —
> `from corrigenda import CorrectionPipeline, BaseProvider, ...` — is
> defined exclusively by `corrigenda.__all__`. Symbols listed below
> that are NOT in `__all__` (e.g. `RewriterMetrics`, `ReconcileMetrics`,
> `plan_page`, `validate_llm_response`, `AcceptanceResult`, …) are
> sub-module-only: they remain importable from their canonical path,
> but `from corrigenda import RewriterMetrics` will raise `ImportError`.

- `corrigenda.formats.alto`: ALTO XML parsing and rewriting (v2/v3/v4), with
  the Hyphenation Reconciler.
  - `parse_alto_file`, `build_document_manifest` *(top-level)*
  - `rewrite_alto_file`, `extract_output_texts` *(top-level)*, `RewriterMetrics` *(sub-module only)*
  - `enrich_chunk_lines`, `reconcile_hyphen_pair`, `ReconcileMetrics`,
    `classify_reconcile_outcome`, `should_stay_in_same_chunk` *(all sub-module only)*
- `corrigenda.core`: chunk planning, LLM-response validation,
  per-line acceptance policy, and `CorrectionPipeline`.
  - `CorrectionPipeline`, `CorrectionResult`, `sanitize_error` *(top-level)*
  - `plan_page`, `downgrade_granularity` *(sub-module only)*
  - `validate_llm_response` *(sub-module only)*
  - `check_line`, `check_adjacent_duplicates`, `AcceptanceResult` *(all sub-module only)*
- `corrigenda.core.protocols`: ports consumers implement.
  - `BaseProvider`, `PipelineObserver`, `OutputWriter` *(top-level)*
  - `OUTPUT_JSON_SCHEMA`, `SYSTEM_PROMPT` *(top-level, home: `corrigenda.producers.llm`)*
- `corrigenda.core.schemas`: domain Pydantic models (manifests, enums, LLM
  payloads, traces, model info). Top-level re-exports cover the
  models consumers typically reach for — see `corrigenda.__all__`.

### Public API guarantees (alpha caveat)
- Importable via the top-level package: `from corrigenda import
  CorrectionPipeline, BaseProvider, parse_alto_file, …` (full list
  in the package `__all__`).
- Each sub-module declares its own `__all__`.
- ARCHITECTURE.md ADR-006: the pipeline never logs by itself — every
  diagnostic is an `observer.on_event(...)` so hosts route them as
  they wish.
- Snapshot tests on a 566-line corpus pin byte-identical rewrite
  output across the 35+ commits of the refactor that produced this
  release.

### Known limitations
- API is still alpha; breaking changes possible until 1.0.
- `CorrectionPipeline.run` accepts `provider_name`/`model`/`api_key`
  individually (server-side legacy); a future release will likely fold
  them into the injected `BaseProvider`.

[Unreleased]: https://github.com/maribakulj/alto-llm-corrector/compare/corrigenda-v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/maribakulj/alto-llm-corrector/releases/tag/corrigenda-v0.1.0a1
