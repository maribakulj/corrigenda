# alto-core

Pure ALTO XML correction pipeline — the algorithmic core of
[alto-llm-corrector](https://github.com/maribakulj/alto-llm-corrector),
extracted so it can be consumed without pulling in the FastAPI server,
the filesystem job store, or the bundled LLM providers.

## Status

**0.1.0a1 — alpha.** The package is shipped as part of the
alto-llm-corrector monorepo. The API may still shift before 1.0.

## What's in the box

- `alto_core.alto` — ALTO XML parsing and rewriting (v2/v3/v4),
  with the Hyphenation Reconciler.
- `alto_core.pipeline` — chunk planning, LLM-response validation,
  per-line acceptance policy, and the pure `CorrectionPipeline` that
  ties them together.
- `alto_core.schemas` — Pydantic models for documents, pages, blocks,
  and lines (domain only; HTTP DTOs live in the server package).
- `alto_core.protocols` — ports (`BaseProvider`, `PipelineObserver`,
  `OutputWriter`) that consumers implement to plug the core into their
  own infrastructure.

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

from alto_core import (
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
        return {
            "lines": [
                {"line_id": line["line_id"], "corrected_text": line["ocr_text"]}
                for line in user_payload["lines"]
            ],
        }


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


asyncio.run(main())
```

## Releasing

The version is read from `src/alto_core/__init__.py::__version__` by
hatchling (single source of truth — `pyproject.toml` is `dynamic`).

To cut a new release:

1. Bump `__version__` in `src/alto_core/__init__.py`.
2. Add a `## [X.Y.Z]` entry to [CHANGELOG.md](./CHANGELOG.md).
3. Commit + tag: `git tag alto-core-vX.Y.Z`.
4. Push the tag.
5. From the GitHub UI, run **Actions → Publish alto-core → Run
   workflow**. Pick `testpypi` first to validate, then `pypi`. The
   workflow uses Trusted Publishing (PEP 740 / OIDC) — no API token
   stored in GitHub secrets.

For a local dry-run before pushing:

```bash
scripts/release-alto-core.sh             # build + smoke-install only
scripts/release-alto-core.sh --testpypi  # build + upload TestPyPI
```

## License

Apache 2.0 (see [LICENSE](./LICENSE)).
