# ADR-004 — Two explicit deployment profiles: `demo` and `institutional`

Status: accepted (2026-07)

## Context
The repo mixed two incompatible security stances: a public HF Space
(no auth, wildcard CORS, volatile storage) and an "institutional"
narrative assuming an SSO/reverse-proxy the app never verifies. Each
stance is legitimate; pretending they are one is not.

## Decision
`DEPLOYMENT_PROFILE=demo` (default) names the public-demo stance and is
documented as such in SECURITY.md. `DEPLOYMENT_PROFILE=institutional`
asserts a proxy in front and REFUSES demo-grade defaults at startup
(today: wildcard `CORS_ORIGINS`). Institutional-only features
(persistence, ownership, quotas) target this profile.

## Consequences
Misconfiguration fails loudly at startup, not silently at runtime. New
security-relevant defaults must decide their behaviour per profile.
