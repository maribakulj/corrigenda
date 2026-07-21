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

## Manifest

`manifest.json`: `corpus_version` (bump on ANY case change — reports
cite it) and `cases[]` with `name`, `format`, `source`, `reference`
(paths relative to this directory), `provenance`.
