# corrigenda

Pure ALTO XML correction pipeline — the algorithmic core of
[alto-llm-corrector](https://github.com/maribakulj/alto-llm-corrector),
extracted so it can be consumed without pulling in the FastAPI server,
the filesystem job store, or the bundled LLM providers.

## Status

**0.1.0a1 — alpha.** The package is shipped as part of the
alto-llm-corrector monorepo. The API may still shift before 1.0.

## What's in the box

- `corrigenda.formats.alto` — ALTO XML parsing and rewriting (v2/v3/v4),
  with the Hyphenation Reconciler.
- `corrigenda.formats.page` — PAGE XML (PRImA/Transkribus/eScriptorium):
  polygon geometry preserved verbatim (bbox derived for the planner),
  canonical text via `TextEquiv @index` with a `Word`-concat fallback,
  heuristic hyphenation (`- ¬ ⸗ U+00AD`), and a rewriter that never
  touches geometry. Both formats produce the **same `DocumentManifest`**.
- `corrigenda.core.editing` + `corrigenda.producers` — the **span edit
  protocol**: `EditScript` / `ReplaceLine` / `ReplaceSpan` with
  `RangeAnchor` and `MatchAnchor`, a deterministic `RulesProducer`, the
  `EditProducer` contract and a vision envelope (the library forwards an
  opaque image reference and touches no pixel). See
  [`docs/edit-protocol.md`](docs/edit-protocol.md).
- `corrigenda.core` — chunk planning, LLM-response validation,
  per-line acceptance policy, and the pure `CorrectionPipeline` that
  ties them together (`run()` async, `run_sync()` façade).
- `corrigenda.core.schemas` — Pydantic models for documents, pages, blocks and
  lines, plus the four **frozen, injectable policies**: `RetryPolicy`
  (attempt cap / temperature ramp / per-chunk budget — `.default()` is
  byte-compatible with the historical behaviour, `.deterministic()` pins
  every temperature to 0), `GuardConfig` (every anti-migration threshold),
  `ChunkPlannerConfig`, and `PairingPolicy` (hyphen-pairing seam). Each
  exposes `policy_fingerprint()`; the pipeline combines them into
  `config_fingerprint()`, stamped into the corrected XML's
  `processingStep` for provenance.
- `corrigenda.errors` — one root, `CorrectionError`, over `ParseError`,
  `ValidationError` (both also `ValueError`) and `CorrectionAborted`
  (raised by the cooperative `should_abort` cancellation probe).
- `CorrectionResult.report` — a public, versioned `CorrectionReport`
  (full per-line trace: source → model in/out → projected → re-extracted
  text, rewriter path, fallback reason). `run(apply=False)` executes the
  whole pipeline without persisting anything — the report is the
  deliverable (dry-run / preview / benchmarking).
- `corrigenda.core.protocols` — ports (`BaseProvider`, `PipelineObserver`,
  `OutputWriter`) that consumers implement to plug the core into their
  own infrastructure.
- PEP 561 `py.typed` marker — the package type-checks under
  `mypy --strict` and so can your integration.

Job-level concepts (`JobManifest`, `JobStatus`, the `Provider` vendor
enum) are deliberately **not** here — the core does not enumerate LLM
vendors or track a server job's lifecycle; they live in the consumer.

## What's not

- No LLM HTTP calls (you supply a `BaseProvider` implementation, or use
  an adapter like XerLLM).
- No filesystem I/O beyond reading source ALTO files.
- No FastAPI, no SSE, no job store — those live in the `alto-server`
  package.

## Minimal working example

```python
import asyncio
from pathlib import Path

from corrigenda import (
    BaseProvider,
    CorrectionPipeline,
    OutputWriter,
    PipelineObserver,
    build_document_manifest,
)


class IdentityProvider:
    """Returns each line's OCR text unchanged — useful for smoke tests."""

    async def list_models(self, api_key):
        return []

    async def complete_structured(
        self, api_key, model, system_prompt, user_payload, json_schema, temperature=0.0,
    ):
        # F14 contract: return (parsed_json, usage). Usage is an
        # corrigenda.core.schemas.Usage (tokens in/out) or None when the
        # provider cannot report consumption.
        return {
            "lines": [
                {"line_id": line["line_id"], "corrected_text": line["ocr_text"]}
                for line in user_payload["lines"]
            ],
        }, None


class PrintObserver:
    def on_event(self, event_type, payload):
        print(f"{event_type}: {payload}")


class FilesystemWriter:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir

    def write_corrected(self, *, source_stem, xml_bytes):
        (self.out_dir / f"{source_stem}_corrected.xml").write_bytes(xml_bytes)

    def write_trace(self, *, traces_payload):
        (self.out_dir / "trace.json").write_text(traces_payload, encoding="utf-8")


async def main():
    src = Path("page.xml")
    doc = build_document_manifest([(src, src.name)])

    pipeline = CorrectionPipeline(
        provider=IdentityProvider(),
        observer=PrintObserver(),
        output_writer=FilesystemWriter(Path("./out")),
    )
    result = await pipeline.run(
        document_manifest=doc,
        api_key="",
        model="mock",
        provider_name="local",
        source_files={src.name: src},
        run_id="local-run",  # optional — auto-generated when omitted
    )
    print(f"reconciled {result.total_reconciled} hyphen pairs across {result.total_chunks} chunks")
    print(f"tokens: {result.usage.total_tokens}; report lines: {result.report.total_lines}")


asyncio.run(main())
```

No event loop of your own? `pipeline.run_sync(...)` takes the same
arguments and wraps `asyncio.run` for you. Pass `apply=False` to either
form for a dry run (nothing written; inspect `result.report`), and
`should_abort=callable` for cooperative cancellation (raises
`CorrectionAborted` between pages/chunks, before any output is written).

## Releasing

The version is read from `src/corrigenda/__init__.py::__version__` by
hatchling (single source of truth — `pyproject.toml` is `dynamic`).

To cut a new release:

1. Bump `__version__` in `src/corrigenda/__init__.py`.
2. Add a `## [X.Y.Z]` entry to [CHANGELOG.md](./CHANGELOG.md).
3. Commit + tag: `git tag corrigenda-vX.Y.Z`.
4. Push the tag.
5. From the GitHub UI, run **Actions → Publish corrigenda → Run
   workflow**. Pick `testpypi` first to validate, then `pypi`. The
   workflow uses Trusted Publishing (PEP 740 / OIDC) — no API token
   stored in GitHub secrets.

For a local dry-run before pushing:

```bash
scripts/release-corrigenda.sh             # build + smoke-install only
scripts/release-corrigenda.sh --testpypi  # build + upload TestPyPI
```

## License

Apache 2.0 (see [LICENSE](./LICENSE)).
