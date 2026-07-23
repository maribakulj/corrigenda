# Changelog

All notable changes to **corrigenda** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **QE scoring + routing brain (ROADMAP V3 Phase 3, core).** New
  ``corrigenda.core.quality``: the ``QEScorer`` protocol (score a
  SOURCE line's need for correction in [0,1], pre-LLM), a
  zero-dependency ``HeuristicQEScorer`` baseline, and the routing brain
  (``RoutingPolicy`` frozen thresholds, ``RoutingDecision`` enum,
  ``route_line`` — SKIP a clean line for no LLM call / LLM / ESCALATE).
  Routing is opt-in: ``RoutingPolicy()`` defaults both bounds to
  ``None`` (every line → LLM, historical behaviour). The heuristic uses
  ONLY orthography-neutral signals (a digit stranded in a word;
  out-of-lexicon against a supplied lexicon) — NOT archaic glyphs:
  measured on the OCR17+ corpus, an archaic-glyph heuristic scored the
  human-corrected reference (full of preserved long-s / u-for-v)
  HIGHER than raw OCR, the exact "flag historical spelling as
  improbable" trap. Without a lexicon the baseline abstains by design;
  distinguishing a real OCR non-word (``cukiuent``) from a valid
  historical form (``cultiuent``) needs a historical lexicon or model —
  the measured justification for the Phase 3 ONNX/D'AlemBERT scorer,
  which will sit behind the same ``QEScorer`` protocol in
  ``corrigenda[qe]``. Additive public API; not yet in the pipeline or
  the §8.2 composite fingerprint (wiring is the next Phase 3 step).

- **LLM uncertainty channel (ROADMAP V3 Phase 1).** Opt-in contract
  variant for ``LLMEditProducer`` (``uncertainty_channel=True``, also
  on ``CorrectionPipeline.for_provider``): the model must return a
  per-line ``status`` (``certain``/``uncertain`` — an explicit outlet
  for doubt instead of silent guessing) and reason-coded per-token
  ``edits`` (``confusion_connue`` / ``mot_du_lexique`` /
  ``infere_du_contexte`` / ``conjecture``). The app VERIFIES every
  verifiable claim (``corrigenda.core.confidence.score_producer_claims``
  — confusion table, lexicon, token existence); a failed check scores
  BELOW an honest conjecture. The verified score rides
  ``ReplaceLine.producer_confidence`` (additive) and feeds the
  ``producer`` component of ``LineConfidence``. Off by default: the
  base prompt/schema stay byte-identical, and the channel's different
  contract yields a different producer ``configuration_fingerprint``
  (§11).

- **Multi-component line confidence (ROADMAP V3 Phase 1).**
  ``ConfidencePolicy(mode="drop"|"report_only")`` — default ``drop``
  keeps behaviour identical; ``report_only`` fills a
  ``LineConfidence`` block on every ``LineOutcome``: named components
  (``ocr`` — the source engine's own confidence, now preserved by both
  parsers as ``LineManifest.ocr_confidence`` from ALTO ``String/@WC``
  mean / PAGE ``TextEquiv/@conf``; ``alignment`` — the token-alignment
  score of the decided text; ``scorers`` — each injected
  ``ConfidenceScorer`` by name; ``producer`` — reserved for the LLM
  uncertainty channel) plus a ``decision`` aggregate under an
  IDENTIFIED formula (``min`` over present components). New
  ``corrigenda.core.confidence`` module with the ``ConfidenceScorer``
  protocol and the zero-dependency ``HeuristicScorer`` (character
  evidence + classic OCR confusion table + optional lexicon).
  ``mode="write_wc"`` is declared but LOCKED (raises) until the
  Phase 2 calibration harness proves the values on a real corpus.
  ``ConfidencePolicy`` deliberately stays OUT of the §8.2 composite
  fingerprint until then (``report_only`` never affects the corrected
  XML) — pinned by test.

- **token_realign loss policy + sidecar (ROADMAP V3 Phase 1).**
  ``LossPolicy`` grew ``min_alignment_score`` (default ``None`` — the
  gate is off and behaviour is identical). When set, a word-count-
  changing correction whose token alignment onto the source scores
  below the threshold — or ANY correction raising the aligner's move
  flag — is not projected: the line reverts to source markup (whole
  hyphen unit, ADR-010) and the correction is PRESERVED as a
  ``SidecarEntry`` on ``CorrectionReport.sidecar`` (also written as
  ``sidecar.json`` by ``CorrectionResult.write`` when non-empty) for
  review instead of lost. New core module ``corrigenda.core.alignment``
  (char-level Levenshtein similarity → monotonic token DP; a match
  requires character evidence; moves are flagged, never applied) also
  drives the ALTO slow path's identity recycling, which is now aligned
  instead of positional. The §11 composite ``config_fingerprint``
  moved (``55dc80679dd71f94`` → ``15dc07cba9122106``): the new FIELD
  joins the fingerprinted policy surface (defaults unchanged).

- **Versioned correction benchmark + ground-truth corpus seed (P4.2).**
  ``scripts/benchmark.py`` (repo tooling, not part of the package)
  measures a producer against ``tests/corpus_gt/`` — micro-averaged
  CER/WER before/after, improved/degraded line counts, false positives
  (already-correct lines a run changed), fallbacks, reconcile outcomes,
  structural losses, latency/page and peak memory — and emits a JSON
  report a release can cite: ``benchmark_version``, ``lib_version``,
  ``corpus_version``, the §11 ``config_fingerprint`` and the producer's
  ``ProducerMetadata`` identity. Three deterministic, offline
  producers: ``rules``, ``oracle`` (cassette derived from the
  reference — the upper bound the guards still arbitrate) and
  ``cassette:<path>`` (recorded ``{line_id: text}`` replay, the P4.2
  LLM-cassette seam). The corpus ships one clearly-marked SYNTHETIC
  seed case (documented scripted degradations: long s, ﬁ ligature, one
  ``rn`` confusion the default rules deliberately cannot fix; one
  heuristic hyphen pair) — real human-reviewed Gallica pages remain
  P4.1's human work, and the corpus README pins the provenance rules.
  House rule now in force: no guard/ramp default changes without a
  measured improvement here. Smoke-tested in CI (report contract,
  rules improvement with residual, oracle at CER 0, cassette ≡ oracle).

- **The error root gets its final name (P3.11, first slice).**
  ``CorrigendaError`` — named for the LIBRARY, like
  ``requests.RequestException`` — replaces ``CorrectionError`` as the
  §8.4 root, and ``ProposalValidationError`` replaces the bare
  ``ValidationError`` (which collided with pydantic's in every
  consumer's imports; the P3.7 vocabulary already calls what it
  validates a *proposal*). The old names remain 0.9.x deprecation
  ALIASES of the very same classes — ``except``, ``isinstance`` and
  subclassing behave identically through either name; machine ``code``
  attributes are unchanged — and disappear at the P3.11 top-level
  reduction. Library internals speak the new names; both spellings are
  top-level exports for now.

- **The three-line happy path (P3.12, §2).**
  ``corrigenda.load(*paths)`` sniffs each file's root namespace (ALTO
  or PAGE — one format per document, unique basenames) and returns a
  ``LoadedDocument`` (manifest + the name → path map a run needs);
  ``corrigenda.correct(document, producer=…)`` /
  ``corrigenda.correct_sync(…)`` run a default pipeline around any
  ``EditProducer`` — no observer, no adapter, no manifest plumbing
  required for the simple case (no-op observer, default policies,
  provenance from the producer's declared identity). Purely ADDITIVE:
  ``CorrectionPipeline`` keeps every knob; the P3.11 top-level API
  reduction remains a separate, deliberate decision. All four symbols
  are lazy top-level exports (``import corrigenda`` still never loads
  lxml); the quickstart now leads with the three-line path.

- **EditScript preconditions — a script only applies to the document
  it was computed against (P3.10, §4).** ``EditScript`` records its
  ``protocol_version`` (``EDIT_PROTOCOL_VERSION``, currently ``"1"``;
  ``apply_edit_script`` raises ``ValidationError`` on a version it
  does not speak), the run's ``source_digests`` (same values as
  ``RunProvenance`` — one shared computation), and one
  ``LinePrecondition`` per op-carrying line: the :func:`line_digest`
  of the SOURCE text the ops were computed against, page-qualified
  like the ops' stamps. Applying a script to a document that carries
  the same line_id over DIFFERENT content rejects the line's ops
  (``precondition_source_digest``) — an op never lands on a lookalike.
  All fields optional/additive: hand-written scripts keep their
  historical behaviour. The run's final ``edit_script`` is fully
  stamped. ``EDIT_PROTOCOL_VERSION``, ``LinePrecondition`` and
  ``line_digest`` join the public surface.

- **`RunProvenance` — the report says exactly what produced it (P3.9,
  §11).** ``CorrectionReport.provenance`` (optional, additive — no
  ``report_version`` bump) records the library version, the §8.2
  composite ``config_fingerprint`` (the same value stamped into the
  XML), the GENERIC producer identity (``ProducerProvenance``, the
  report-side mirror of ``ProducerMetadata`` — a rules run records its
  name + configuration digest, never an artificial vendor/model pair),
  a ``sha256:`` digest of every INPUT file's bytes (empty on dry
  runs), the manifest's source format, and the installed versions of
  the critical dependencies (lxml, pydantic — resolved from package
  metadata, never by importing them: the pure core stays lxml-free).
  Generation parameters are not duplicated: the retry/temperature
  strategy is already covered by the policy fingerprint.
  ``RunProvenance`` and ``ProducerProvenance`` join the public surface.

- **`LossPolicy` — REPORT or STRICT on format-granularity loss
  (ADR-012, P3.8).** The PAGE rewriter cannot keep ``Word`` geometry
  when a correction changes a line's word count (6.2 P4 slow path).
  ``LossPolicy(strict=False)`` (the default) makes the historical
  stance explicit: the correction projects, the loss is counted
  run-wide (``CorrectionReport.format_losses``) and now ATTRIBUTED per
  decision — ``ProjectionStage.losses`` carries each line's own share
  (``RewriteResult.losses_by_line`` → ``LineTrace.projection_losses``
  under the hood). ``LossPolicy(strict=True)`` rejects instead: a
  correction that cannot project without loss makes its WHOLE hyphen
  unit fall back to source text (reason code ``format_loss``,
  ADR-010 atomicity) BEFORE any output exists, so the source markup
  keeps its word geometry. The check runs in the pure core off the new
  ``LineManifest.word_count`` (stamped by the PAGE parser; ``None`` on
  word-less lines and on ALTO, whose per-token geometry redistributes
  at any count). Stale-annotation drops (``conf``, alternative
  ``TextEquiv``, offset ``custom`` groups) describe the old reading and
  stay report-only in both modes; no third mode until a real need
  shows up. ``LossPolicy`` joins the §8.2 policy surface, the public
  API, ``for_provider``, and the §11 ``config_fingerprint`` (composite
  pin ``216aa712f1e99b79`` → ``55dc80679dd71f94`` — a fifth ``loss``
  key in the payload).

### Changed

- **BREAKING — `ProducerMetadata` replaces the bare
  `provider_name`/`model` strings (P3.7, fourth slice).** The
  ``CorrectionPipeline`` constructor takes one
  ``producer_metadata: ProducerMetadata | None`` (frozen dataclass:
  ``name``, ``version``, ``implementation``,
  ``configuration_fingerprint``) instead of the two label kwargs — a
  rules producer has no "model", so the generic identity names WHO
  produced the edits and, when one exists, the concrete engine behind
  the name. Producers may DECLARE their identity via an optional
  ``metadata`` attribute (the ``requires_full_coverage`` convention;
  explicit constructor metadata wins): ``LLMEditProducer`` declares
  ``name="llm"`` + its model, ``RulesProducer`` declares
  ``name="rules"`` plus a deterministic 16-hex
  ``configuration_fingerprint`` over its rules table and lexicon.
  ``for_provider`` keeps its pinned vendor vocabulary
  (``provider_name``/``model``) and builds the envelope itself, and the
  §11 labels stamped into corrected XML derive via
  ``provenance_labels()`` (an implementation-less producer stamps
  ``"unknown"``) — the format seam and the stamped bytes are unchanged.
  ``ProducerMetadata`` joins the public surface.

- **BREAKING — the producer seam takes `ProducerOptions`, not the
  RetryPolicy (P3.7, first slice).** ``EditProducer.produce(payload, *,
  options)`` receives a per-call envelope — ``attempt``, the RESOLVED
  ``temperature`` (ramp and hyphen 0.0-pin decided engine-side, ending
  the "policy whose first temperature is this attempt's" contortion),
  an optional ``deadline_seconds`` hint, and ``should_abort``: the
  run's cancellation probe, so a producer can abandon long I/O
  mid-flight (or wire the probe into its HTTP client) instead of the
  engine only noticing between chunks. The engine keeps the full
  ``RetryPolicy`` to itself.

- **Client/catalog split (P3.7, second slice).** ``BaseProvider`` is
  now the composition of two protocols: ``StructuredCompletionClient``
  (``complete_structured`` — the ONLY LLM capability the core consumes;
  ``LLMEditProducer`` and ``for_provider`` type against it, so a client
  with no ``list_models`` at all drives a full run) and
  ``ModelCatalog`` (``list_models`` — application vocabulary; the demo
  backend's ``/providers/{p}/models`` concern). Both join the public
  surface; ``BaseProvider`` keeps working unchanged for full vendor
  clients.

- **BREAKING — generic vocabulary replaces the LLM-branded names
  (P3.7, third slice).** ``LLMUserPayload`` → ``CorrectionRequest``,
  ``LLMLineInput`` → ``LineContext``, ``LLMLineOutput`` →
  ``LineProposal``, ``LLMResponse`` → ``ProposalBatch`` — a rules or
  vision producer receives no "LLM payload"; the edit protocol's
  request/proposal shapes are producer-agnostic. The purely-LLM
  contract (``SYSTEM_PROMPT``, ``OUTPUT_JSON_SCHEMA``) moves from
  ``corrigenda.producers.llm`` to ``corrigenda.integrations.llm`` —
  ``corrigenda.producers`` keeps only producer implementations. No
  wire/JSON shape changes anywhere: these are Python-surface renames.
  Remaining P3.7 work: ``ProducerMetadata`` replacing bare
  ``provider_name``/``model`` (a rules producer has no "model") — the
  provenance-stamping surface, a later slice.

- **BREAKING — `PipelineEventType` names only engine events (P3.6,
  first slice).** The server-side values — job lifecycle ``started`` /
  ``completed`` / ``failed`` / ``cancelled``, the frontend-only
  ``queued``, and the SSE transport ``keepalive`` / ``error`` — left
  the engine's enum for the demo backend's own
  ``app.jobs.events.JobEventType``: the library no longer enumerates
  what a HOST says about a job. The wire strings are unchanged on both
  sides (the SSE contract test still pins the union against the
  frontend's list); only the Python spelling of the server values
  moved.

- **Typed engine events (P3.6, second slice).**
  ``corrigenda.core.events`` defines one frozen ``EngineEvent``
  dataclass per ``PipelineEventType`` (``DocumentParsed``,
  ``ChunkStarted``, ``ChunkDowngraded``, ``RewriterStats``, …): the
  emit sites construct these instead of ad-hoc dict literals, so every
  payload's shape lives in exactly one importable place and the
  type↔class bijection is pinned by test. The observer port keeps its
  wire shape (``on_event(event_type, payload)``) — the pipeline renders
  ``event.type`` + ``event.payload()`` at the boundary, so observers
  and the SSE wire format are untouched. The demo backend's job-end
  ``reconcile_stats`` now goes through the typed ``ReconcileStats``
  too.

- **BREAKING — report v2: staged `LineOutcome` entries (P3.5,
  `report_version` 1.0 → 2.0).** `CorrectionReport.lines` now carries
  one `LineOutcome` per line — ``source_text`` plus three explicit
  stages: ``proposal`` (producer input/output, absent when the line
  never reached a producer), ``decision`` (terminal ``status``,
  ``final_text``, and a STRUCTURED ``reason`` ``{code, detail}`` whose
  code aggregates exactly like ``CorrectionResult.fallback_reasons``),
  and ``projection`` (``extracted_text`` — renamed from
  ``output_alto_text``, wrong in an ALTO+PAGE library — and
  ``rewriter_path``; absent when no output file was rendered). The
  builder reads the ADR-011 `DecisionSet` (the terminal stage's
  authority), completing slice C's reader migration. The decision stage
  additionally carries ``features`` (`ProposalFeatures`) — the
  similarity/length metrics the acceptance guard computed ONCE while
  deciding (recorded on ``AcceptanceResult.features``), so no consumer
  re-derives them; absent on lines that never reached per-line
  acceptance. `LineOutcome`, `ProposalStage`, `ProposalFeatures`,
  `DecisionStage`, `DecisionReason` and
  `ProjectionStage` join the public surface; `LineTrace` remains the
  Python-side working trace on ``CorrectionResult.traces`` — the two
  surfaces version independently (``docs/versioning.md``).

- **BREAKING — `run()` never mutates its input (ADR-011, slice E).**
  The engine works on its own deep copy of the document manifest: the
  caller's manifest keeps its parse-time state (`corrected_text` stays
  `None`, `status` stays `PENDING`), re-running the same document
  always starts from the original OCR text, and the run's outcome is
  read off the result — `CorrectionResult.decisions`, an immutable
  `DecisionSet` with one terminal `LineDecision` per line in reading
  order (`DecisionSet`, `LineDecision` and `LineRef` join the public
  surface). Consumers that displayed corrected text from the manifest
  project the decisions onto their own state (as the demo backend now
  does for its /diff and /layout read models).

### Removed

- **The one-run-per-instance guard (ADR-005, superseded).** With
  per-run state fully contained (fresh `RunContext` + private manifest
  copy) and no writer on the engine, concurrent `run()` calls on one
  instance are safe and supported; the `RuntimeError` guard is gone.
  The P0 run-independence property is now pinned in its final form:
  two runs on the SAME document object yield identical decisions.

- **BREAKING — persistence left the engine surface (ADR-011, slice
  D-fin).** `CorrectionPipeline(output_writer=…)` /
  `for_provider(output_writer=…)` and `run(apply=…)` are gone, and the
  `OutputWriter` protocol is no longer part of `corrigenda` (the demo
  backend now owns its own port in `app.protocols`). The engine never
  writes: every run computes `result.corrected_files` + `result.report`
  and the caller persists — `result.write(dir)` for the simple case
  (corrected XML under the source names + `report.json`), or a
  host-owned transaction. What `apply=False` used to buy is now every
  run's behaviour. The ADR-005 one-run-per-instance guard remains (its
  surviving rationale: shared observer + in-place manifest mutation
  until slice E).

### Added

- **The result carries its artefacts (ADR-011, slice D).**
  `CorrectionResult.corrected_files` maps each source file name to its
  corrected XML bytes on EVERY run — dry runs included, where the bytes
  were previously unreachable — and `result.write(dir)` persists the
  artefacts plus the §9 report (`report.json`) caller-side.

- **Terminal-decision invariant — no line ends a run undecided.** The
  page loop's ADR-008 absorb branch (recoverable `CorrectionError` →
  `chunk_error` event + continue) now OCR-falls-back every
  still-`PENDING` target line of the failed chunk before continuing —
  previously those lines silently kept no decision while the run
  reported success. A run-level backstop additionally refuses to write
  outputs while any line is `PENDING` (engine bug → loud `RuntimeError`,
  never a degraded success).

- **Projection invariant — the artefact must say what the run decided.**
  The per-line text re-extracted from the rewritten XML (previously a
  trace-only diagnostic, `output_alto_text`) is now verified against the
  final per-line decision before the writer persists anything. A missing
  line or a word-level divergence raises the new
  `corrigenda.errors.ProjectionError` and fails the run — a divergent
  artefact is corruption, never a valid output. Whitespace runs are
  compared in normal form: ALTO/PAGE word tokenization cannot represent
  consecutive spaces, a documented format property (exact-space loss
  accounting is future loss-policy work).

### Fixed

- **An identity proposal can no longer be rejected as hyphen fusion.**
  The Stage-A fusion detector flagged a PART1/BOTH line whenever its
  last corrected word equalled the pair's logical word — even when the
  SOURCE line already ended with that word (degenerate one-letter
  fragments: 'A' + 'A' → word 'AA' on a line reading 'AA-'). A producer
  proposing the source verbatim was rejected on every retry, the chunk
  hard-failed, and the descent budget OCR-fell-back every cohabiting
  line, with a blast radius that depended on the chunk partition — how
  the chunking-invariance gate caught it. The check is now
  source-relative, like every other drift check: it only fires when the
  correction *introduced* the full word.

- **A fallback now covers the whole hyphen unit, across pages
  (ADR-010).** A cross-page pair lives in two page-scoped chunks; when
  one side's chunk fell back while the other side's succeeded, the pair
  ended half OCR / half corrected — the joined word across the seam was
  rewritten on one line and kept verbatim on the other, the exact state
  `reconcile_hyphen_pair`'s contract forbids. Both directions are
  closed: a chunk fallback (and the absorb branch) extends to the
  unit's members on other pages via the shared hyphen closure, and the
  reconcile/acceptance paths refuse to correct a member whose partner
  already fell back (`hyphen_partner_fell_back` /
  `hyphen_unit_fallback` trace reasons). The duplicate-revert pass now
  walks that same shared closure instead of its own inline worklist —
  one traversal, one definition of the unit.

### Added

- **`corrigenda.core.units` — atomic hyphen groups (ADR-010, slice 1).**
  `HyphenGroup` + `derive_hyphen_groups()` are THE single derivation of
  "these lines travel together" (maximal hyphen components, members as
  `LineRef`s in reading order, `spans_pages`/`explicit` flags),
  cross-validated property-by-property against a new rich generated
  corpus (chains PART1→BOTH→PART2, multi-page files, explicit
  cross-page seam pairs). The chunk planner's window pinning now
  consumes it; its local union-find is gone. Reconciliation/fallback
  per group follows in the next slice.

### Changed

- **The format seam returns one `RewriteResult` (ADR-011, slice A).**
  `FormatAdapter.rewrite_file` now returns `RewriteResult(xml_bytes,
  metrics, rewriter_paths, texts, losses)`; the adapter-level
  `extract_texts` port is gone. The per-line texts are read off the
  very tree the bytes were serialized from, so the projection
  invariant (P1.4) verifies without re-parsing the output — one full
  lxml parse per file removed from every run. `RewriteResult` unpacks
  positionally to the historical `(xml_bytes, metrics, rewriter_paths)`
  triple during the migration. The module-level `extract_output_texts`
  helpers remain for round-trip checks over arbitrary bytes.

- **The run's decisions materialize as an immutable `DecisionSet`
  (ADR-011, slice C).** After the global consistency pass —
  the point where no later pass may change a decision — the engine
  derives `corrigenda.core.decisions.DecisionSet`: every line's
  terminal decision (source text, final text, status, fallback reason)
  in document reading order, keyed by qualified identity. The
  terminality backstop is now its construction invariant (a `PENDING`
  line refuses materialization and fails the run), and the projection
  invariant plus the result's `fallback_lines`/`fallback_reasons`
  accounting read the DecisionSet instead of re-walking the mutable
  manifests. Internal for now; it is the seam the immutable-source
  slice flips.

- **Manifest counters are computed (ADR-011, slice B).**
  `DocumentManifest.total_pages/total_blocks/total_lines` derive from
  the pages (`computed_field` — still present in the serialized shape);
  they are no longer constructor inputs and the contradictory-totals
  validator is retired: a derived count cannot lie. Constructors
  passing the legacy kwargs keep working — the values are ignored.

- **`CorrectionReport.format_losses` is finally populated.** The field
  existed since the PAGE rewriter grew its granularity counters
  (`words_dropped`, `custom_offset_stripped`, …) but no pipeline run
  ever set it. The `RewriteResult` carries each file's counters and the
  run aggregates them onto the report — dry-run included.

- **One global consistency pass (P3.3).** Adjacent-duplicate detection
  now runs ONCE over the whole document in canonical reading order
  (pages in manifest order, lines in page order, never across source
  files), keyed by qualified line identity. It replaces three partial
  sweeps — the intra-chunk sweep, the cross-chunk boundary pass and the
  page-seam pass — that each carried their own comparison base, and the
  seam pass's ambiguity skip on colliding bare ids is gone with them.
  Chunk finalization no longer reverts anything, so the pass compares
  every line's live pre-revert accepted correction on one basis, and a
  rejection pulls the whole hyphen unit via the shared derivation. Two
  lines adjacent inside a merged multi-block chunk but not adjacent on
  the page are no longer spuriously compared. Note: per-page
  `page_completed` events now report provisional correction counts —
  final decisions are made by the global pass; the report and the
  result remain authoritative.

- **Hyphen reconciliation is unit-driven (ADR-010, slice 2 complete).**
  A chunk's target lines and their resolved partners are handed to
  `derive_hyphen_groups` — the single derivation of "these lines travel
  together" — and each unit's joins reconcile with one walk in reading
  order. This replaces the two role-keyed passes (PART1→partner, then
  BOTH→forward) that re-derived the grouping from pointer fields at
  every step. The planner's block packing likewise merges blocks
  through the derivation (its per-link pointer walk is gone). Outcomes
  are unchanged; planner pinning, block packing, fallback closure,
  duplicate reverts and reconciliation all consume the one derivation.

- **The planner's over-cap chain cut is a recorded unit operation
  (ADR-010).** Severing the forward link of a chain longer than
  `max_lines_per_request` now goes through
  `corrigenda.core.units.split_forward_link` — the single writer for
  link removal — and each cut is recorded as a `HyphenSplit` on the
  `ChunkPlan` instead of happening as a silent pointer side effect
  inside the planner. Behaviour is unchanged; the cut is now visible
  to consumers of the plan.

- **Page images are keyed by page, not by file (breaking).**
  `run(source_images={source_name: ref})` gave every page of a multipage
  XML the SAME image — the vision producer looked at page 1's scan for
  every page but the first. The parameter is now
  `run(page_images={page_id: ref})` (page ids are document-unique,
  ADR-007), coverage is verified PER PAGE by the renamed
  `require_page_images` (raising `ConfigurationError`, no longer
  `ValidationError` — this is composition, not producer output), and a
  key matching no page (e.g. a legacy file-name key) is refused
  explicitly instead of silently reproducing the old behaviour.
- **Document-wide line lookups are keyed by `LineRef` (ADR-009,
  breaking).** New frozen dataclass `corrigenda.core.identity.LineRef`
  (`page_id`, `line_id` — fully qualifying under ADR-007's
  document-unique page ids) replaces the engine's three ad-hoc key
  shapes: hand-built composite strings (traces, pre-revert snapshots,
  finalization owners), raw `(page_id, line_id)` tuples (producer-op
  capture, cross-page hyphen indexes) and the string keys of
  `CorrectionResult.traces`, which now maps `LineRef → LineTrace`. A
  cross-page keying mistake is now a type error, not a runtime
  overwrite.
- **Recoverability is an allowlist (breaking for non-conforming
  providers).** The producer-attempt path re-raised eight known
  programmer-bug types and degraded EVERYTHING else to
  retry-then-OCR-fallback: a `RuntimeError` from a producer bug or a
  raw SDK transport exception nobody wrapped ended as a "successful"
  run with silently uncorrected text. Recoverable is now exactly what
  the retry classifier can route — `ProviderTransientError` and the
  `ValueError` family (`ValidationError`, `HyphenIntegrityError`,
  `json.JSONDecodeError`) — and everything else fails the run.
  Consequence: wrapping transport failures as `ProviderTransientError`
  is now the provider CONTRACT (`BaseProvider` docstring says MUST),
  enforced by failing loudly. ADR-008 revised.
- **The format travels with the document — no implicit ALTO default
  (breaking for hand-built manifests).** The parsers stamp
  `DocumentManifest.source_format` ("alto" / "page") and the engine
  derives the matching adapter from it at write time: a PAGE document
  now corrects end-to-end with no adapter injected (following the
  quickstart's PAGE hint used to rewrite PAGE with the ALTO rewriter).
  An injected adapter that contradicts the manifest's format raises the
  new `ConfigurationError` at run start; a hand-built manifest (no
  stamped format) reaching the write phase without an explicit adapter
  raises it too, instead of silently assuming ALTO.
- **Fallback accounting counts LINES, not chunks (breaking).**
  `CorrectionResult.fallback_count` (bumped once per fallen *chunk* — a
  rejected 20-line chunk reported "1") is renamed `fallback_chunks`, and
  two fields join it: `fallback_lines` — the number of lines whose
  terminal status is `FALLBACK` (manifest statuses are the authority:
  chunk fallbacks, acceptance-guard rejections and duplicate reverts all
  count) — and `fallback_reasons`, the aggregated reason prefixes per
  line. Consumers deciding "completed vs completed-with-fallbacks" must
  use `fallback_lines`: the old counter reported 0 for a guard-rejected
  line that silently kept its OCR text.
- **Provider errors join the single-root hierarchy.**
  `ProviderTransientError` and `ProviderPermanentError` (still importable
  from `corrigenda.core.protocols`) now derive from the new
  `corrigenda.errors.ProviderError`, itself a `CorrectionError` — the
  documented "catch the root once" contract previously excluded exactly
  the errors a mis-configured run raises first. Behaviour is unchanged:
  permanent rejections stay fatal for the run (explicit re-raise handlers
  precede every absorbing branch; pinned by `tests/test_error_taxonomy.py`).
- **Machine-readable error metadata.** Every error class carries a stable
  snake_case `code` and a `retryable` class flag so hosts route on
  structure instead of message text.

## [0.9.0] — 2026-07-16

### Changed

- **Re-versioned `1.0.0` → `0.9.0`, classifier `Production/Stable` →
  `Beta`.** Nothing was ever published to an index under `1.0.0` (the
  tag was never created — the release plan requires an independent
  external API review first, and the core refactor planned in
  `docs/audit/PLAN-1.0-2026-07-15.md` will deliberately break the API
  beforehand). A version that promises a frozen surface while breaks are
  planned is dishonest; the 0.9.x series says what it is. The section
  below keeps its original date and content — it describes the same
  code, renumbered.

## [0.9.0 initial scope, formerly "1.0.0"] — 2026-07-15

First complete release candidate scope. Everything below shipped together —
nothing was ever published under an earlier name or number,
so there is no deprecation layer anywhere: final import paths and final
schemas from day one. The public surface is pinned by an executable
snapshot test (`tests/test_public_api_snapshot.py`); strict SemVer starts
at `1.0.0` (see `docs/versioning.md`).

Highlights: ALTO **and PAGE XML** backends producing one common
`DocumentManifest`; the §4 span edit protocol (`EditScript`,
`ReplaceLine`/`ReplaceSpan`, `MatchAnchor`→`RangeAnchor`); producers as
first-class citizens (`EditProducer`, deterministic `RulesProducer`, LLM
adapter, vision envelope with zero pixel I/O); the versioned
`CorrectionReport` as the single trace artefact; four frozen, fingerprinted
policies; byte-parity golden gates over a real BnF/Transkribus corpus.


### Audit remediation — 37 findings + per-wave adversarial reviews (2026-07-12 → 15)

The exhaustive audit (`docs/audit/AUDIT-2026-07-13.md`) and its
wave-by-wave remediation (`docs/audit/PLAN-CORRECTIONS.md`) landed as part
of 1.0 — each fix reproduced by a failing test first, each wave reviewed
adversarially with its findings treated before the next.

### Fixed (audit F1-F12 + adversarial-review follow-ups, 2026-07-13)

- **Lines-never-merge, heuristic mode (Audit-F1).** The PART2 word-growth
  guard now protects EVERY reconcile accept path — explicit-with-subs,
  explicit-without-subs and heuristic — so a short heuristic PART2 can no
  longer absorb words from the following physical line.
- **Hyphen-chain revert atomicity (Audit-F2).** The duplicate-revert
  partner extension runs to fixpoint: whole 3+/4+-line chains
  (PART1→BOTH→…→PART2) revert together instead of leaving a mixed
  OCR+corrected pair.
- **Duplicate guard across seams (Audit-F3 + review).** The cross-chunk
  boundary pass and the page-seam pass compare PRE-revert accepted
  corrections (run-level snapshot), and boundary owners are the chunks
  that ACTUALLY finalized each line — granularity-descent sub-chunk
  seams (including single-chunk plans) are now covered.
- **Dry-run edit_script attribution (Audit-F4 + review).**
  ``_producer_ops`` is keyed by ``(page_id, line_id)`` so files reusing
  bare line ids no longer corrupt each other's ops; emitted ops carry an
  optional ``page_id`` and ``apply_edit_script(page_id=…)`` scopes a
  multi-file replay (additive — no report_version bump).
- **ALTO rewriter (Audit-F5/F6 + review).** The single-String BOTH guard
  is shared by ``_apply_subs`` and ``_subs_need_update`` (identity lines
  classify UNTOUCHED again); the slow-path rebuild trims edge whitespace
  before tokenizing (children tile the line exactly); the original HYP's
  WIDTH is parsed via the shared tolerant policy (an ``1e999`` value
  aborted the whole rewrite with an uncaught OverflowError).
- **Numeric parsing policy (Audit-F7/F8/F9).** ``parse_int_tolerant``
  treats inf/overflow-shaped values by contract — default in tolerant
  mode, ``ValueError`` in strict — shared by the ALTO ``_int_attr`` and
  the PAGE ``polygon_to_bbox`` (which skips non-finite pairs atomically).
- **Single-line invariant (Audit-F10).** The validator and the edit
  protocol reject the full ``str.splitlines`` separator repertoire
  (U+2028/U+2029, ``\x0b``, ``\x0c``, ``\x85``, …), not just ``\n``/``\r``.
- **Rules producer lexicon guard (Audit-F11).** Composed edits inside one
  token are re-validated as a whole against the lexicon; a composition
  that leaves it is rejected as a batch.
- **PAGE custom attribute verbatim slices (Audit-F12).** Kept groups are
  verbatim source slices (byte-identical round-trip for spacing).

### Changed (wave-3 review, 2026-07-13)

- ``CorrectionPipeline._write_outputs`` offloads ``rewrite_file`` /
  ``write_corrected`` / ``extract_texts`` to worker threads: a large
  rewrite no longer freezes the host's event loop (SSE keepalives,
  health checks). Observer events remain on the loop. ``run()`` is
  unchanged API-wise.

### Fixed (exhaustive audit — library correctness cluster, 2026-07-12)

- **Hyphen reconciliation.** The explicit-mode subs join stripped only
  ASCII `-`, so an explicit pair whose break char is `¬`/`⸗`/soft-hyphen
  (Fraktur/old print) never matched its `SUBS_CONTENT` and was
  systematically reverted to OCR — the join now strips the full
  `HYPHEN_CHARS` repertoire (matching the widened trailing-hyphen gate).
  Separately, an explicit-mode PART2 that absorbed trailing words from the
  next line (`"saires"` → `"saires du roi"`) could pass the boundary-word
  join and survive as a merged line; PART2 word growth now forces a
  fallback, preserving the "lines never merge" invariant.
- **Edit protocol (E2).** A zero-length insertion co-located with a
  replacement's start offset escaped the overlap check and, applied
  right-to-left in an ambiguous order, could leave a character the
  replacement was meant to remove — co-located span ops are now rejected as
  overlaps.
- **ALTO rewriter.** (a) A heuristically-detected PART1 (trailing dash, no
  explicit markup) no longer gets a synthesised `<HYP>` or a phantom
  trailing hyphen on the slow path — the conservative-heuristic invariant.
  (b) A single-`String` `BOTH` line keeps its backward `HypPart2` marker
  instead of the forward `HypPart1` write clobbering the same element.
  (c) *(byte change)* the slow-path rebuild reserves the trailing HYP's
  real width and repositions it flush at the line's right edge, so the
  child widths sum exactly to the line `WIDTH` with no overlap (previously
  the HYP kept its stale HPOS/WIDTH while the Strings were laid over a 4%
  estimate). The scripted byte-parity goldens move accordingly; identity
  goldens are unchanged.
- **LLM-response validator.** The hyphen fusion check now honours
  `target_line_ids`: in F8 window mode a hyphen pair sitting entirely in a
  chunk's *context* region can no longer fail the whole chunk (which
  discarded the chunk's valid *target* corrections on retry/fallback).
- **`PairingPolicy.same_block_only`** is page-qualified, honouring its
  documented cross-page guarantee when block ids repeat across pages (both
  pages exporting `TextBlock1`).
- **Adjacent-duplicate guard.** A run of three or more identical
  corrections now reverts every member; the loop used to skip the third.
- **PAGE `polygon_to_bbox`.** A half-malformed `x,y` pair (good `x`, bad
  `y`) is skipped atomically instead of leaving a dangling `x` that
  inflated the bbox.
- **`RulesProducer` lexicon guard** normalises through `ncfold` (NFC +
  casefold), so a decomposed (NFD) lexicon entry matches the parser's
  NFC-normalised tokens (previously a silently missed guarded correction).
- **Parsers refuse an id-less `TextLine`** (both ALTO and PAGE): a
  fabricated manifest id cannot round-trip through the rewriter (it matches
  on the real id attribute), so its correction would be silently dropped —
  the file is now rejected with `ParseError` instead. An id-less region
  under a `ReadingOrder` keeps document order (conservative bail).
- **Pipeline.** The cross-page duplicate-revert now reaches a hyphen
  partner living on another page, so a reconciled cross-page pair reverts
  atomically (never half OCR / half corrected). The page-seam duplicate
  pass compares a seam only within one source file. `CorrectionResult.
  edit_script` is rebuilt from the final per-line state (after
  reconciliation, acceptance and every revert), so a dry-run consumer
  replaying it reproduces the pipeline's own output — it never carries a
  stale op for a line reverted to OCR or reconciled to different text; an
  accepted-unchanged line keeps the producer's original op type. The
  producer-attempt error guard uses a denylist of genuine programming-error
  types, so a real bug (KeyError/TypeError/…) fails the run instead of
  silently degrading every chunk to OCR, while provider transport /
  validation errors still degrade.

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
