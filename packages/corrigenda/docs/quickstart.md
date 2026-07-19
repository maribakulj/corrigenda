# Quickstart

`corrigenda` corrects post-OCR text in heritage transcription XML (ALTO
and PAGE) **without ever touching the document's structure**: lines are
never merged, moved or resegmented; geometry is never rewritten on PAGE
and only word boxes are redistributed on ALTO. The producer proposes,
the library decides.

A complete, runnable version of everything below ships as
[`examples/quickstart.py`](../examples/quickstart.py) — it runs offline
against the repo's `examples/sample.xml` (and is executed by the test
suite, so it cannot rot).

## Install

```bash
pip install corrigenda        # Python ≥ 3.11; pydantic v2 + lxml
```

`import corrigenda` never loads lxml — the parsers/rewriters materialise
lazily on first use, so core-only consumers (guards, planner, edit
protocol) can run where lxml isn't installed.

## The three-line path (§2)

```python
import corrigenda
from corrigenda import RulesProducer, default_french_ocr_rules

document = corrigenda.load("page.xml")        # ALTO or PAGE, by namespace
result = corrigenda.correct_sync(             # `await corrigenda.correct(...)` in async code
    document, producer=RulesProducer(default_french_ocr_rules())
)
result.write("out/")                          # corrected XML + report.json
```

No observer, no adapter, no manifest plumbing: `load()` sniffs the root
namespace and parses; `correct()`/`correct_sync()` run a default
pipeline (no-op observer, default policies, provenance from the
producer's declared identity). Any `EditProducer` fits — the LLM path
below plugs in the same way. Every knob the façade hides (policies,
observer, explicit metadata) lives on `CorrectionPipeline`.

## Parse → correct → write

```python
import asyncio
from pathlib import Path

from corrigenda import CorrectionPipeline, build_document_manifest


class MyProvider:
    """Any LLM client with these two methods (see BaseProvider)."""

    async def list_models(self, api_key): ...

    async def complete_structured(
        self, api_key, model, system_prompt, user_payload, json_schema, temperature=0.0
    ):
        # Return ({"lines": [{"line_id": ..., "corrected_text": ...}]}, Usage|None)
        ...


class Observer:
    def on_event(self, event_type, payload):
        print(event_type, payload)


async def main():
    src = Path("my_page.xml")
    doc = build_document_manifest([(src, src.name)])   # ALTO
    # PAGE XML: import build_document_manifest from
    # corrigenda.formats.page.parser instead — nothing else changes:
    # the manifest carries its format and the pipeline derives the
    # matching rewriter from it (no adapter to inject).

    pipeline = CorrectionPipeline.for_provider(
        MyProvider(),
        api_key="sk-…",          # credentials live in the producer, never on run()
        model="my-model",
        provider_name="my-vendor",
        observer=Observer(),
    )
    result = await pipeline.run(
        document_manifest=doc,
        source_files={src.name: src},
    )
    result.write(Path("out"))   # corrected XML + report.json — your call
    print(result.report.total_lines, "lines;",
          result.usage.total_tokens, "tokens")

asyncio.run(main())
```

No event loop of your own? `pipeline.run_sync(...)` takes the same
arguments. The engine itself never writes (ADR-011): the returned
result — corrected bytes, `CorrectionReport`, normalized `EditScript` —
is the deliverable, and `result.write(dir)` persists it when you want
it on disk; `should_abort=callable` gives cooperative cancellation.

## Deterministic pre-pass (no LLM at all)

```python
from corrigenda import CorrectionPipeline, RulesProducer, default_french_ocr_rules

pipeline = CorrectionPipeline(
    producer=RulesProducer(default_french_ocr_rules()),   # ſ→s, ﬁ/ﬂ …
    observer=Observer(),
)
```

The rules engine emits exact-offset `replace_span` ops — reproducible to
the byte, zero network. It declares its own provenance
(`ProducerMetadata(name="rules", configuration_fingerprint=…)` — a rules
engine has no "model"), which the §11 `processingStep` stamp picks up
automatically; pass `producer_metadata=ProducerMetadata(...)` to the
constructor to override it. See [the edit protocol](edit-protocol.md) for the
op model, and [formats](formats.md) for what each rewriter guarantees.

## Reading the results

- `result.corrected_files` — the corrected XML bytes per source file
  name; `result.write(dir)` writes them (plus `report.json`).
- `result.report` — the versioned **CorrectionReport** (§9, v2): one
  staged `LineOutcome` per line — `source_text`, `proposal` (producer
  in/out), `decision` (status, final text, structured reason),
  `projection` (extracted text, rewriter path).
- `result.edit_script` — the normalized EditScript the run applied.
- `result.usage` — aggregated tokens (F14); `Usage(0, 0)` when the
  producer doesn't report.
- `result.decisions` — the immutable **DecisionSet**: one terminal
  decision per line (`by_ref[LineRef(page_id, line_id)]` →
  `final_text` / `status` / `fallback_reason`). The manifests you
  passed in are never modified (ADR-011): the same document can be run
  again — or concurrently — and always starts from the original OCR
  text.
