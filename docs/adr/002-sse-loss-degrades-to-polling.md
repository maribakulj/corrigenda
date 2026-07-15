# ADR-002 — A lost SSE stream degrades to polling, never fails the job

Status: accepted (2026-07)

## Context
Exhausted SSE reconnects used to set the client's job status to
`failed` without consulting the server. The job often kept running and
succeeded; the UI gated download/diff/layout on `completed`, so the
real result became unreachable. Transport state and job state are
different things.

## Decision
`useJobStream` keeps a separate `streamState`
(`live/reconnecting/polling`). After MAX_RETRIES consecutive stream
failures it polls `GET /api/jobs/{id}` (5 s); only the server's verdict
— or a 404 — is terminal. A banner offers manual stream reconnection.
Terminal statistics come from structured payloads (SSE `completed`
event or the status snapshot), never parsed out of log text.

## Consequences
The status endpoint is the source of truth; SSE is an optimisation.
Network failures during polling log once and keep polling.
