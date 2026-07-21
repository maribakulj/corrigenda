# corrigenda

Structure-safe post-OCR correction of heritage transcriptions — **ALTO**
and **PAGE XML** — by LLM, rules engine, or any custom `EditProducer`.
The algorithmic core of
[alto-llm-corrector](https://github.com/maribakulj/alto-llm-corrector),
consumable without the FastAPI server, the job store, or the bundled LLM
providers. *Corrigenda*: the printed errata leaf bound into books —
literally what this library produces.

## Status

**0.9.0 — beta.** The public surface is pinned by an executable
snapshot test, but the API is **not frozen yet**: the 0.9.x series may
break it deliberately (each break is a reviewed snapshot change with a
CHANGELOG entry). Strict SemVer starts at `1.0.0`, which requires an
independent external API review first; see
[docs/versioning.md](docs/versioning.md). Docs:
[quickstart](docs/quickstart.md) ·
[edit protocol](docs/edit-protocol.md) ·
[formats](docs/formats.md) — and a runnable, test-guarded
[examples/quickstart.py](examples/quickstart.py).

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
- `CorrectionResult` — the run's whole deliverable (ADR-011): the
  corrected XML per source file (`result.corrected_files`), the
  immutable per-line `DecisionSet` (`result.decisions`), a public,
  versioned `CorrectionReport` (v2: one staged `LineOutcome` per line —
  source → proposal → decision → projection, with structured fallback
  reasons), the applied `EditScript` and the run's statistics. The
  engine never persists anything and never mutates its input — the
  same document can be run again or concurrently; `result.write(dir)`
  is the one-call persistence helper, or feed the bytes to your own
  transaction.
- `corrigenda.core.protocols` — ports (`BaseProvider`,
  `PipelineObserver`, `FormatAdapter`) that consumers implement to plug
  the core into their own infrastructure.
- PEP 561 `py.typed` marker — the package type-checks under
  `mypy --strict` and so can your integration.

Job-level concepts (`JobManifest`, `JobStatus`, the `Provider` vendor
enum) are deliberately **not** here — the core does not enumerate LLM
vendors or track a server job's lifecycle; they live in the consumer.

## What's not

- No LLM HTTP calls (you supply a `BaseProvider` implementation, or use
  an adapter like XerLLM).
- No filesystem writes, ever — reading source ALTO files is the only
  I/O; outputs travel on `CorrectionResult` (ADR-011).
- No FastAPI, no SSE, no job store — those live in the `alto-server`
  package.

## Minimal working example

```python
import asyncio
from pathlib import Path

from corrigenda import (
    BaseProvider,
    CorrectionPipeline,
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


async def main():
    src = Path("page.xml")
    doc = build_document_manifest([(src, src.name)])

    # §5.1 — the pipeline is built around an EditProducer; credentials live
    # inside the producer, never on run(). for_provider() wraps a raw LLM
    # BaseProvider for you. (A deterministic RulesProducer, or any custom
    # EditProducer, goes through CorrectionPipeline(producer=...) directly.)
    pipeline = CorrectionPipeline.for_provider(
        IdentityProvider(),
        api_key="",
        model="mock",
        provider_name="local",
        observer=PrintObserver(),
    )
    result = await pipeline.run(
        document_manifest=doc,
        source_files={src.name: src},
        run_id="local-run",  # optional — auto-generated when omitted
    )
    # The engine never writes; the result carries the artefacts.
    result.write(Path("./out"))  # corrected XML + report.json
    print(f"reconciled {result.total_reconciled} hyphen pairs across {result.total_chunks} chunks")
    print(f"tokens: {result.usage.total_tokens}; report lines: {result.report.total_lines}")


asyncio.run(main())
```

No event loop of your own? `pipeline.run_sync(...)` takes the same
arguments and wraps `asyncio.run` for you. Every run is effectively a
dry run until *you* persist (`result.write(dir)` or your own sink); pass
`should_abort=callable` for cooperative cancellation (raises
`CorrectionAborted` between pages/chunks — no result, nothing to
persist).

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
