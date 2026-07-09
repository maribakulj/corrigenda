---
title: Corrigenda
emoji: 📄
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---

# Corrigenda

Post-OCR text correction of ALTO XML files using LLM providers (OpenAI, Anthropic, Mistral, Google Gemini).

Upload one or more ALTO XML files, choose a provider and model, and get corrected ALTO XML back — with hyphenation pairs preserved intact across line boundaries.

**What it does:** corrects OCR errors in ALTO `<String CONTENT="..."/>` elements.
**What it does not:** OCR, resegmentation, line merging/splitting, translation, or text modernisation.

---

## Documentation map

The correction engine has been extracted into a standalone library
(`packages/corrigenda/`); this repo is that library **plus** a FastAPI +
React app around it. Start with the authoritative docs; the rest are design
history kept for provenance.

**Authoritative (kept current):**

| Doc | Scope |
|---|---|
| `README.md` (this file) | The app: what it does, how to run and deploy it |
| `packages/corrigenda/docs/` | The library: `quickstart`, `formats`, `edit-protocol`, `versioning` |
| `packages/corrigenda/CHANGELOG.md` | The library's released changes (SemVer) |
| `SPECS_LIB_V2.md` | Normative spec for the `corrigenda` library |
| `SPECS_API.md` / `SPECS_JOBS.md` / `SPECS_FRONTEND.md` | Backend / jobs / frontend specs |
| `CONTRIBUTING.md`, `CLAUDE.md` | Contributor + assistant guidance |

**Historical (design & audit trail — non-normative; may name modules that
have since moved, e.g. the pre-extraction `backend/app/alto/*` layout):**
`SPECS.md` (original app spec), `ARCHITECTURE.md`, `MIGRATION.md`,
`AUDIT.md`, `ISSUE_LEDGER.md`, `REMEDIATION_STATUS.md`, `PLAN_V2.md`,
`PROGRESS_V1.md`, `ROADMAP.md`, and the topic `SPECS_*` drafts. Read them for
*why* a decision was made, not for *where* code lives today.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) 24+
- [Docker Compose](https://docs.docker.com/compose/) v2+

---

## Local installation

```bash
git clone https://github.com/maribakulj/alto-llm-corrector.git
cd alto-llm-corrector

# Copy the example env file (edit if needed)
cp .env.example .env

# Build and start both services
docker compose up --build
```

The app is then available at **http://localhost:5173**.
The backend API is exposed at **http://localhost:8000**.

To stop:

```bash
docker compose down
```

---

## Deployment on Hugging Face Spaces

1. Create a new Space on [huggingface.co/spaces](https://huggingface.co/spaces) with **Docker** as the SDK.
2. Push this repository to the Space:

```bash
git remote add space https://huggingface.co/spaces/<your-username>/<space-name>
git push space main
```

The root `Dockerfile` is detected automatically. It builds the React frontend and embeds it as static files served by FastAPI on **port 7860** (required by HF Spaces).

No separate nginx is needed — FastAPI serves `/` from `./static/` and the SPA catch-all returns `index.html`.

### ⚠ Job storage is volatile

The container writes uploads and corrected outputs to `/tmp/app-jobs/<job_id>/`. **Anything in `/tmp` is lost when the container restarts** (HF Spaces redeploys on every commit, on idle eviction, and on factory reboot). Practical implications:

- A job in progress when the Space redeploys is killed and the result is lost.
- The `trace.json` and corrected XML are gone after a restart even if the job completed — download them immediately.
- A user revisiting the Space after a restart will get a `404` on `/api/jobs/{id}/download` for any previous job_id.

The frontend shows a yellow warning banner above the upload zone. If you need persistence, mount a persistent volume (paid HF Spaces feature) and point `JOB_STORAGE_DIR` to it:

```
ENV JOB_STORAGE_DIR=/data/app-jobs
```

(or set the env var in the Space settings UI).

Single-worker on purpose — see Dockerfile comments. A multi-worker setup would need a shared `JobStore` (Redis, Postgres) since the current one is in-process.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `JOB_STORAGE_DIR` | `/tmp/app-jobs` | Base directory for job files (input + output) |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed CORS origins, or `*` |

---

## Hyphenation Reconciler

ALTO files often encode inter-line hyphenation via `SUBS_TYPE="HypPart1/HypPart2"` and `SUBS_CONTENT` attributes, or via a trailing dash heuristic. The **Hyphenation Reconciler** (`corrigenda.core.hyphenation`, in the `packages/corrigenda` library) treats such pairs as atomic units:

- Both lines are always sent in the **same LLM chunk** — never split across requests.
- The LLM is instructed to correct each line individually without moving text between them.
- After the LLM response, the reconciler redistributes the corrected fragments back onto the original physical lines and reconstructs the `HYP`/`SUBS_*` attributes.
- On ambiguity or repeated failure the original OCR text is kept as fallback.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, FastAPI, Pydantic v2, httpx, lxml, sse-starlette |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS |
| LLM providers | OpenAI, Anthropic, Mistral, Google Gemini |
| Dev stack | docker-compose (backend :8000 + nginx :5173) |
| HF Spaces | Single multi-stage Dockerfile, port 7860 |
| Storage | `/tmp/app-jobs/{job_id}/` — no database |
