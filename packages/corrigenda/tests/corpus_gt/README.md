# Ground-truth corpus (P4.1/P4.2)

Reference transcriptions the benchmark (`scripts/benchmark.py`) measures
against. Every case pairs a **source** file (what OCR produced) with a
**reference** file (what the text should read), same format, same line
IDs.

## Provenance rules

- **Real ground truth** requires a *human-reviewed* reference
  transcription (P4.1: 10–20 stratified Gallica pages — ALTO + PAGE,
  book + press, explicit/heuristic/cross-page/chained hyphenation,
  early and modern French). That cost is human and deliberate: never
  commit a machine-generated file as "reference".
- **Synthetic cases** (name prefixed `synthetic-`) bootstrap the
  benchmark before the human corpus lands: the reference is written for
  this corpus, and the source is derived from it by *scripted,
  documented degradations*. They validate the measurement pipeline;
  they do not validate the library against real OCR.

## Current cases

- `synthetic-fr-early-print` — 6 lines of early-modern-flavoured
  French, one heuristic hyphen pair (`trou-` / `blât …`). Degradations
  applied to derive the source from the reference: non-final `s` → `ſ`
  (long s), `fi` → `ﬁ` (ligature) — both fixable by
  `default_french_ocr_rules()` — plus one `m` → `rn` confusion
  (`moindre` → `rnoindre`) that the default rules deliberately cannot
  fix without a lexicon, so the rules producer keeps a measurable
  residual CER and the oracle producer erases it.
- `ocr17-descartes-discours-p14` / `ocr17-lafayette-cleves-p11`
  (**real**, PAGE, ROADMAP V3 Phase 2) — genuine Transkribus OCR of two
  17th-century French prose pages vs the upstream HUMAN-corrected
  reference, from **OCR17+** (Simon Gabay et al., e-ditiones —
  **CC-BY**, attribution kept here and in
  `examples/page/PROVENANCE.md`). Upstream artifact handled by
  `derive_ocr17.py` (committed outputs, offline suite): the raw
  export's line-level `TextEquiv` already carries the corrected
  reading, so the derivation re-exposes the real OCR (`cukiuent`,
  `eft`…) from the `Word` elements at line level — deterministic
  re-exposure of upstream data, nothing machine-generated. The
  references are the upstream human corrections, satisfying the
  provenance rule above.

  **Not yet registered in `manifest.json`** — and that gap is itself a
  measurement: the very first oracle run over these pages exposed a
  real decision/artifact coherence bug (2026-07-23). The source line
  ends in `-` (raw Word OCR) while the reference ends in `¬`; the PAGE
  rewriter's P5 pass forces the SOURCE break character back onto the
  corrected text *after* the decision recorded `¬`, so the artefact
  diverges from the decision and `_verify_projection` rightly raises
  `ProjectionError`. P5 must be enforced BEFORE the decision
  materializes (decision == artefact, always). Registering these cases
  (corpus_version bump) is the acceptance test of that fix. Measured
  meanwhile with `scripts/benchmark.py` on a locally-extended
  manifest: `default_french_ocr_rules()` corrects NOTHING on this real
  OCR (CER 0.069→0.069 and 0.133→0.133, zero false positives) — the
  honest baseline the synthetic case could not provide.

## Manifest

`manifest.json`: `corpus_version` (bump on ANY case change — reports
cite it) and `cases[]` with `name`, `format`, `source`, `reference`
(paths relative to this directory), `provenance`.
