# ADR-003 — Capability tokens are header-only; URLs carry scoped signed credentials

Status: accepted (2026-07)

## Context
The per-job capability token rode `?token=` for the surfaces that
cannot set headers (EventSource, `<img>`, download links). Query
strings leak into reverse-proxy/ingress/APM logs — exactly the layer an
institutional deployment sits behind and the app cannot redact. A
leaked token gave full job access (download, diff, trace).

## Decision
The token is accepted ONLY via `X-Job-Token`; `?token=` 404s.
Header-less surfaces use short-lived HMAC credentials
(`app/api/signed_urls.py`) scoped to one job AND one purpose:
`events_url` minted at creation (lifetime = run timeout + margin),
images `?sig=` (15 min) appended by the layout endpoint. Downloads go
through fetch + header + blob. The signing secret is per-process — a
restart invalidates URLs, acceptable for an in-memory job store.

## Consequences
A leaked events URL can only watch progress events. curl users pass the
header. If the store ever becomes persistent/multi-process, the signing
secret must move to shared config.
