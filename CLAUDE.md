# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Corrigenda is a post-OCR text-correction **library** (`packages/corrigenda/`, supports ALTO and PAGE XML) plus a demo **web app** (FastAPI backend + React frontend) around it, using LLM providers (OpenAI, Anthropic, Mistral, Google Gemini). Users upload ALTO/PAGE XML, pick a provider/model, and get corrected XML back. It does NOT do OCR, resegmentation, line merging/splitting, translation, or text modernization.

Normative docs: `README.md`, `SPECS_LIB_V2.md`, `packages/corrigenda/docs/`, `docs/API.md`, `SECURITY.md`, `CONTRIBUTING.md`. Everything under `docs/history/` is frozen history — never trust it for current module locations, and never update it to match code.

## Tech Stack

- **Library:** Python 3.11+, Pydantic v2, lxml, httpx — no FastAPI/server dependency
- **Backend:** FastAPI, uvicorn, sse-starlette (flat `app` package, not built/packaged)
- **Frontend:** React + TypeScript + Vite + Tailwind CSS (`corrigenda-frontend`)
- **Deployment:** docker-compose (dev: backend:8000 + frontend:5173) or single Dockerfile for HF Spaces (port 7860, frontend built as static files served by FastAPI). `DEPLOYMENT_PROFILE=demo|institutional` (see SECURITY.md)
- **Storage:** `{JOB_STORAGE_DIR:-/tmp/app-jobs}/{job_id}/` on disk, job state in memory, no database. Orphan job dirs are reclaimed at startup

## Common Commands

```bash
# Library
cd packages/corrigenda
pytest                          # coverage gate 85%
mypy --strict src/corrigenda

# Backend
cd backend
pip install -e ../packages/corrigenda && pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest -m "not e2e"             # coverage gate 80% on `app`
pytest tests/e2e                # real uvicorn + fake provider
pytest tests/test_store.py::test_name -v  # single test

# Frontend
cd frontend
npm install
npm run dev     # dev server on :5173
npx vitest run && npx tsc --noEmit && npm run lint
npm run build   # production build

# OpenAPI snapshot + generated TS types (CI fails on drift)
scripts/generate-frontend-api-types.sh

# Docker
docker-compose up               # full local dev stack
docker build -t corrigenda .    # HF Spaces single container
```

## Architecture

### Core Pipeline (in `packages/corrigenda/src/corrigenda/`)

The correction flow is: **Parse → Chunk → Enrich → LLM Call → Validate → Reconcile → Rewrite**

1. `formats/alto/parser.py` (and `formats/page/`) — Parses ALTO XML (v2/v3/v4) / PAGE XML into the common `DocumentManifest`/`PageManifest` structures; detects inter-line hyphenation (explicit via SUBS_TYPE/HYP and heuristic via trailing dash, vetted by `core/pairing.py`)
2. `core/planner.py` — Splits lines into LLM-sized chunks using adaptive granularity: PAGE → BLOCK → WINDOW → LINE. Hyphen pairs are atomic and must never be split across chunks
3. `core/hyphenation.py` — **Hyphenation Reconciler**. Enriches chunks with hyphenation metadata before the LLM call, then reconciles corrected text back onto physical line pairs after the response. Core invariant: the app decides, the LLM informs — lines are never merged or moved
4. `core/validator.py` — Validates LLM JSON responses (line count, IDs, no newlines). Extra check: hyphen pairs must not have been merged by the LLM
5. `core/pipeline.py` — `CorrectionPipeline`, the engine. Retry logic (3 attempts per chunk, then granularity downgrade, then fallback to OCR source text), guards (`core/guards.py`), edit protocol (`core/editing.py`), cooperative cancellation via `should_abort`. **`run()` never mutates its input (private deep copy, ADR-011); instances are reentrant — read outcomes off `result.decisions`**
6. `formats/alto/rewriter.py` — Rewrites ALTO XML with corrected text, reconstructing HYP/SUBS_* elements for hyphen pairs. Never modifies TextLine geometry attributes (ID, HPOS, VPOS, WIDTH, HEIGHT)

Line identity is always **(page_id, line_id)** — line_id alone repeats across files. This holds in the library, the API read models, and the frontend (`frontend/src/lib/lineKey.ts`).

### Backend (`backend/app/`)

- `api/jobs.py` — job endpoints; upload-slot reservation before reading bodies; uploads stream to disk in 1 MiB chunks
- `api/signed_urls.py` — capability tokens travel ONLY in the `X-Job-Token` header; header-less surfaces (EventSource, `<img>`) use short-lived signed `?sig=` credentials scoped to job + purpose
- `jobs/store.py` — in-memory JobStore (SSE fan-out, TTL eviction, startup orphan reclaim)
- `jobs/runner.py` — drives `CorrectionPipeline`, owns job lifecycle incl. `cancelled`
- `jobs/cancellation.py` — per-job cancel events for `POST /api/jobs/{id}/cancel`
- `providers/` — `BaseProvider` implementations (`list_models()`, `complete_structured()`), structured JSON output with provider-specific fallbacks; system prompt in `base.py` (rule 13: correct hyphenated lines individually)

### API surface

See `docs/API.md`; the OpenAPI schema is the contract (CI drift-checks `frontend/openapi.snapshot.json` and `frontend/src/types/api.generated.ts`; frontend REST types alias the generated ones).

## Critical Design Rules

- **Hyphen pairs are atomic**: PART1+PART2 lines must always stay in the same LLM chunk. The chunk planner, validator, and reconciler all enforce this.
- **Lines never merge**: No text migrates between lines. The rewriter preserves physical line boundaries.
- **Line identity is (page_id, line_id)** everywhere — never key anything on line_id alone.
- **Conservative heuristic mode**: When hyphenation is detected heuristically (no SUBS_TYPE in source), no SUBS_CONTENT is invented.
- **Fallback to source**: On ambiguity or repeated LLM failure, always fall back to original OCR text rather than guessing.
- **ALTO geometry**: The rewriter redistributes token widths proportionally within a TextLine but never changes the TextLine's own coordinates.
- **A lost SSE stream is never a job failure**: the frontend falls back to status polling; only the server's verdict (or 404) is terminal.
- **Tokens never in URLs**: header or scoped `?sig=` only.
- **Tests**: every fix ships with the test that fails before it. Audit-trail references (`Audit-Fxx`, waves) stay in PRs/issues, not in new code comments.
