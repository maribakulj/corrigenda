# Backend HTTP API

**Source of truth: the OpenAPI schema**, served live at `/docs` and
`/openapi.json`, and committed as `frontend/openapi.snapshot.json`
(CI regenerates it and fails on drift — see
`scripts/generate-frontend-api-types.sh`). This page is a map, not a
second contract; when in doubt, the schema wins.

## Authentication model

`POST /api/jobs` returns a **capability token** once (`job_token`);
only its SHA-256 hash is stored. Every job endpoint requires it via the
`X-Job-Token` header. Missing/wrong token → **404** (job existence
never leaks). The token is **never accepted in a URL**.

Header-less surfaces use short-lived signed credentials (`?sig=`),
scoped to one job and one purpose:

- **events** — `POST /api/jobs` also returns `events_url`
  (an events-scoped `?sig=` valid for the run's timeout budget), for
  `EventSource`.
- **images** — `GET …/layout` appends a 15-minute images-scoped
  `?sig=` to each `image_url`, for `<img>`.

## Routes

| Route | Purpose |
|---|---|
| `POST /api/providers/models` | List models for a provider + API key |
| `POST /api/jobs` | Multipart upload (`files`, `provider`, `api_key`, `model`, optional `geometric_pairing`) → `{job_id, job_token, events_url}` |
| `GET /api/jobs/{id}` | Authoritative status snapshot (`JobStatusResponse`) |
| `POST /api/jobs/{id}/cancel` | Cooperative cancellation — idempotent, 202, body = current status |
| `GET /api/jobs/{id}/events` | SSE stream (auth via `?sig=` or header) |
| `GET /api/jobs/{id}/download` | Corrected XML (single file) or ZIP |
| `GET /api/jobs/{id}/trace` | The run's versioned `CorrectionReport` (per-line traces) |
| `GET /api/jobs/{id}/diff` | OCR vs corrected, per page/line |
| `GET /api/jobs/{id}/layout` | Blocks/lines with ALTO coordinates + signed `image_url`s |
| `GET /api/jobs/{id}/images/{name}` | Source scan image (auth via `?sig=` or header) |
| `GET /health`, `/health/live`, `/health/ready` | Probes — `ready` includes storage, frontend (when promised) and load gauges |

Job state machine: `queued → started → running → completed |
completed_with_fallbacks | failed`, plus `cancel_requested → cancelled`.
Terminal jobs are evicted after a TTL (default 1 h); artefacts live
under `{JOB_STORAGE_DIR}/{job_id}/` (`input/`, `output/`, `images/`).

## SSE events

Event names are defined by `corrigenda.core.schemas.PipelineEventType`
(the enum is the wire contract) and mirrored by
`frontend/src/hooks/useJobStream.ts::EVENTS`;
`backend/tests/test_sse_event_contract.py` fails CI on any drift.
Terminal events (`completed`, `failed`, `cancelled`) have guaranteed
delivery and are synthesised for late subscribers.
