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


class Writer:
    def write_corrected(self, *, source_stem, xml_bytes):
        Path(f"out/{source_stem}_corrected.xml").write_bytes(xml_bytes)

    def write_trace(self, *, traces_payload):
        Path("out/trace.json").write_text(traces_payload, encoding="utf-8")


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
        output_writer=Writer(),
    )
    result = await pipeline.run(
        document_manifest=doc,
        source_files={src.name: src},
    )
    print(result.report.total_lines, "lines;",
          result.usage.total_tokens, "tokens")

asyncio.run(main())
```

No event loop of your own? `pipeline.run_sync(...)` takes the same
arguments. `apply=False` runs everything without writing (the returned
`CorrectionReport` + normalized `EditScript` are the deliverable);
`should_abort=callable` gives cooperative cancellation.

## Deterministic pre-pass (no LLM at all)

```python
from corrigenda import CorrectionPipeline, RulesProducer, default_french_ocr_rules

pipeline = CorrectionPipeline(
    producer=RulesProducer(default_french_ocr_rules()),   # ſ→s, ﬁ/ﬂ …
    observer=Observer(),
    output_writer=Writer(),
    provider_name="rules", model="fr-ocr-v1",             # provenance labels
)
```

The rules engine emits exact-offset `replace_span` ops — reproducible to
the byte, zero network. See [the edit protocol](edit-protocol.md) for the
op model, and [formats](formats.md) for what each rewriter guarantees.

## Reading the results

- `result.report` — the versioned **CorrectionReport** (§9): per-line
  journey (source → model in/out → projected → re-extracted), rewriter
  path, fallback reason. `trace.json` on disk is this exact JSON.
- `result.edit_script` — the normalized EditScript the run applied.
- `result.usage` — aggregated tokens (F14); `Usage(0, 0)` when the
  producer doesn't report.
- Corrected text also lives on the manifests you passed in:
  `line.corrected_text` / `line.status`.
