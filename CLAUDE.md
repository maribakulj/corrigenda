# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ALTO LLM Corrector is a web app for post-OCR text correction of ALTO XML files using LLM providers (OpenAI, Anthropic, Mistral, Google Gemini). Users upload ALTO XML, pick a provider/model, and get corrected ALTO XML back. It does NOT do OCR, resegmentation, line merging/splitting, translation, or text modernization.

## Tech Stack

- **Backend:** Python 3.11+, FastAPI, Pydantic v2, httpx, lxml, uvicorn, sse-starlette
- **Frontend:** React + TypeScript + Vite + Tailwind CSS
- **Deployment:** docker-compose (dev: backend:8000 + frontend:5173) or single Dockerfile for HF Spaces (port 7860, frontend built as static files served by FastAPI)
- **Storage:** `/tmp/app-jobs/{job_id}/` on disk, job state in memory, no database

## Common Commands

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
pytest                          # run all tests
pytest tests/test_parser.py     # single test file
pytest tests/test_parser.py::test_name -v  # single test

# Frontend
cd frontend
npm install
npm run dev     # dev server on :5173
npm run build   # production build

# Docker
docker-compose up               # full local dev stack
docker build -t alto-corrector . # HF Spaces single container
```

## Architecture

### Core Pipeline

The correction flow is: **Parse → Chunk → Enrich → LLM Call → Validate → Reconcile → Rewrite**

1. `alto/parser.py` — Parses ALTO XML (v2/v3/v4), extracts pages/blocks/lines into `PageManifest` structures, detects inter-line hyphenation (explicit via SUBS_TYPE/HYP and heuristic via trailing dash)
2. `jobs/chunk_planner.py` — Splits lines into LLM-sized chunks using adaptive granularity: PAGE → BLOCK → WINDOW → LINE. Hyphen pairs are atomic and must never be split across chunks
3. `alto/hyphenation.py` — **Hyphenation Reconciler** (central module). Enriches chunks with hyphenation metadata before LLM call (`enrich_chunk_lines`), then reconciles corrected text back onto physical line pairs after LLM response (`reconcile_hyphen_pair`). Core invariant: the app decides, the LLM informs — lines are never merged or moved
4. `jobs/validator.py` — Validates LLM JSON responses (line count, IDs, no newlines). Extra check: hyphen pairs must not have been merged by the LLM
5. `jobs/orchestrator.py` — Main engine. Runs the chunk pipeline with retry logic (3 attempts per chunk, then granularity downgrade, then fallback to OCR source text)
6. `alto/rewriter.py` — Rewrites ALTO XML with corrected text, reconstructing HYP/SUBS_* elements for hyphen pairs. Never modifies TextLine geometry attributes (ID, HPOS, VPOS, WIDTH, HEIGHT)

### LLM Providers (`providers/`)

All providers implement `BaseProvider` protocol with `list_models()` and `complete_structured()`. Each uses structured JSON output (json_schema response format) with provider-specific fallbacks. The system prompt (in `base.py`) has 13 rules; rule 13 explicitly instructs the LLM to correct hyphenated lines individually without moving text between them.

### API Layer (`api/`)

- `POST /api/providers/models` — List available models for a provider+key
- `POST /api/jobs` — Upload files + start correction (multipart)
- `GET /api/jobs/{job_id}` — Poll status
- `GET /api/jobs/{job_id}/events` — SSE stream for real-time progress
- `GET /api/jobs/{job_id}/download` — Download corrected XML/ZIP

### Key Data Models (`schemas/__init__.py`)

- `LineManifest` — Core line representation with hyphenation fields: `hyphen_role` (NONE/PART1/PART2), `hyphen_pair_line_id`, `hyphen_subs_content`, `hyphen_source_explicit`
- `LLMLineInput` — Enriched line sent to LLM with context (prev/next text) and hyphenation metadata
- `JobManifest` / `DocumentManifest` / `PageManifest` — Job tracking hierarchy

## Critical Design Rules

- **Hyphen pairs are atomic**: PART1+PART2 lines must always stay in the same LLM chunk. The chunk planner, validator, and reconciler all enforce this.
- **Lines never merge**: No text migrates between lines. The rewriter preserves physical line boundaries.
- **Conservative heuristic mode**: When hyphenation is detected heuristically (no SUBS_TYPE in source), no SUBS_CONTENT is invented.
- **Fallback to source**: On ambiguity or repeated LLM failure, always fall back to original OCR text rather than guessing.
- **ALTO geometry**: The rewriter redistributes token widths proportionally within a TextLine but never changes the TextLine's own coordinates.
