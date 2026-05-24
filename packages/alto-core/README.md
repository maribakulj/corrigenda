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
  lines, and LLM payloads.
- `alto_core.protocols` — ports (`BaseProvider`, `PipelineObserver`,
  `OutputWriter`) that consumers implement to plug the core into their
  own infrastructure.

## What's not

- No LLM HTTP calls (you supply a `BaseProvider` implementation, or use
  an adapter like XerLLM).
- No filesystem I/O beyond reading source ALTO files.
- No FastAPI, no SSE, no job store — those live in the `alto-server`
  package.

## License

MIT.
