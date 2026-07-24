# The span edit protocol (§4 / §5)

`corrigenda` corrects a transcription by turning a producer's response into
an **`EditScript`** — a list of edit operations against line text — and
applying it back onto the manifests the rewriter knows how to serialise.
The seam sits between the compiler (`enrich_chunk_lines` + payload) and the
recomposer (the format rewriters):

```
structure (ALTO/PAGE)
   │  parse (formats/*)
   ▼
DocumentManifest ──compile──▶ ModelPayload ──producer──▶ EditScript
   ▲                                                        │ normalize + validate (E1–E6)
   └──────────── recompose (formats/*) ◀── edited text ◀────┘
```

## Operations

Two operations, and **no structural op** — there is no `merge_lines`,
`split_line` or `move_text`, so invariant **I2** (text never travels
without its anchor) holds by type, not by a runtime check.

```python
from corrigenda import ReplaceLine, ReplaceSpan, RangeAnchor, MatchAnchor, EditScript

# Whole line (the historical LLM response, re-expressed as one op).
ReplaceLine(line_id="tl_4", text="Velque appro¬")

# A sub-range of the line's canonical text.
ReplaceSpan(line_id="tl_2", anchor=RangeAnchor(start=0, end=1), text="s")
ReplaceSpan(line_id="tl_2", anchor=MatchAnchor(match="ſ"), text="s")
```

## Anchors — two modes, one after normalisation

- **`RangeAnchor(start, end)`** — offsets into the line's *canonical* text
  (the parser's `ocr_text`, format-independent). Deterministic producers
  compute these exactly.
- **`MatchAnchor(match, occurrence=None)`** — an exact substring. LLMs are
  reliable at "replace *this* substring" and unreliable at numeric offsets,
  so this is the LLM-facing form. It **normalises** to a `RangeAnchor`
  against the canonical text; an unfound match, an out-of-range
  `occurrence`, or an ambiguous default (`occurrence=None` — i.e. the
  producer said nothing — matching more than once) **rejects the op** —
  the line keeps its prior text (I2 fallback). An **explicit** integer,
  including `0` for "the first occurrence", always selects that
  occurrence (P2-8: `0` used to double as the unspecified default, making
  the first of several repeats inexpressible).

```python
from corrigenda import normalize_anchor
normalize_anchor(MatchAnchor(match="lo"), "helo world")   # (RangeAnchor(3, 5), None)
normalize_anchor(MatchAnchor(match="o"), "helo world")    # (None, "anchor_ambiguous")
```

## Invariants (E1–E6)

Applied by `apply_edit_script`:

| # | Rule |
|---|------|
| **E1** | every `line_id` is inside the targeted chunk |
| **E2** | a line's normalised spans do not overlap; applied right-to-left |
| **E3** | `text` has no newline; the resulting line is non-empty (a span may delete: `text=""` is allowed if the line survives) |
| **E4** | per-op drift bounds (`GuardConfig.edit_span_max_growth_ratio`, `edit_line_max_changed_chars`) — **`replace_span` only** |
| **E5** | a hyphenated line edited by span keeps its trailing hyphen / boundary word — **`replace_span` only** |
| **E6** | the existing three-stage guard matrix runs on the resulting line text, identically for both ops (applied later by the pipeline) |

**E4/E5 never touch `ReplaceLine`.** The whole-line path is governed by the
same guard matrix (E6) it always was, which is what makes re-expressing
today's response as `replace_line` ops **byte-for-byte identical** (proved
on the corpus in `tests/test_editing.py`).

```python
from corrigenda import apply_edit_script
result = apply_edit_script(
    EditScript(ops=[ReplaceSpan(line_id="l1", anchor=RangeAnchor(0, 1), text="s")]),
    canonical_by_id={"l1": "ſciences"},
)
result.text_by_id      # {"l1": "sciences"}
result.rejected        # []  (typed EditRejection list otherwise)
```

## Producers (§5)

A producer returns `(EditScript, Usage | None)` and declares whether it
needs the physical anchor envelope:

```python
class EditProducer(Protocol):
    wants_geometry: bool
    wants_image: bool
    async def produce(self, payload, *, policy) -> tuple[EditScript, Usage | None]: ...
```

- **LLM** (`LLMEditProducer`) — wraps a `BaseProvider`; emits `replace_line`
  (v1). A `replace_span` LLM output is gated to a later release behind a
  CER/cost bench.
- **Rules** (`RulesProducer`, §5.3) — a deterministic substitution engine
  emitting `replace_span` + `RangeAnchor` with exact offsets. Zero deps,
  reproducible to the byte; a free pre-LLM pass and the protocol's
  reference-test producer.

```python
from corrigenda import RulesProducer, default_french_ocr_rules, apply_edit_script
prod = RulesProducer(default_french_ocr_rules())          # ſ→s, ﬁ/ﬂ ligatures
script = prod.build_edit_script({"l1": "ſoleil"})
apply_edit_script(script, {"l1": "ſoleil"}).text_by_id     # {"l1": "soleil"}
```

- **Vision / VLM** (envelope only in v1, §5.2 bis) — the compiler copies
  per-line `geometry` (coords + page dimensions) and a page `image_ref`
  into the payload *only* when the producer asks. The library
  **never opens a pixel** (invariant **I4**, enforced by
  `test_edit_producer.py::test_i4_no_image_libraries_in_corrigenda`);
  loading/cropping/encoding belongs to the out-of-lib producer.
  `run(page_images=…)` forwards the mapping verbatim — keyed by
  **page_id** (document-unique, one image per physical page, never per
  source file); a `wants_image` producer with a page left uncovered is a
  start-up `ConfigurationError` (`require_page_images`).

  Each `page_images` value is a `PageImage`: the historical **opaque**
  `ImageRef` (str — path/URL/handle) *or*, recommended since ROADMAP V3
  Phase 4, a structured **`ImageAsset`** carrying the provenance the
  audit trail wants (`sha256` of the bytes, decoded `media_type` and
  pixel dimensions, multipage `frame_index`, `exif_orientation`, and an
  `ImageTransform` mapping XML coordinates onto image pixels). Either
  rides the envelope identically and is forwarded verbatim; the core
  still opens neither. The core only *carries* an `ImageAsset` — the
  builder that decodes a file to populate it is the `corrigenda[vision]`
  extra, never the core (I4). An `ImageAsset` whose `page_id` disagrees
  with its mapping key is rejected at start-up.

## Dry run

Every `run()` is side-effect-free (ADR-011 — the engine never
persists): it returns the normalized `EditScript` it applied on
`CorrectionResult.edit_script` (plus the `CorrectionReport` and the
corrected bytes) — the deliverable for preview and benchmarking. A run
becomes "wet" only when the caller persists the result
(`result.write(dir)` or its own sink).

## What the protocol does *not* carry

Hyphenation — roles, pairs, reconciliation — stays a matter of the line
manifests (the core layer), invisible in the `EditScript`. A producer sees
the hyphen hints in the payload and edits each line separately; the
reconciler judges the result.
