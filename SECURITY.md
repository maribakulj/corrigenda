# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** via GitHub's
"Report a vulnerability" (Security → Advisories → Report a
vulnerability) on this repository. Do not open a public issue for
security reports. You should receive an acknowledgement within a week.
There is no bug-bounty programme.

Supported scope: the latest commit on `main`. The `corrigenda` Python
library and the demo application are maintained together; there are no
long-term support branches.

## Deployment profiles and threat model

The application ships with two explicit profiles, selected by the
`DEPLOYMENT_PROFILE` environment variable. **The security guarantees
differ — pick deliberately.**

### `demo` (default — public Hugging Face Space)

What it is: a public demonstration. Treat it accordingly.

- **No user authentication.** Anyone with the Space URL can submit
  jobs. Per-job isolation relies on a capability token returned once at
  creation (only its SHA-256 hash is stored; it travels exclusively in
  the `X-Job-Token` header — never in URLs). Header-less surfaces
  (EventSource, `<img>`) use short-lived HMAC credentials scoped to one
  job and one purpose.
- **Your documents and your LLM API key transit through the server.**
  The key is used for provider calls and never persisted, and error
  messages are sanitised, but you are still trusting the host. **Do not
  submit sensitive or personal documents, and prefer a dedicated,
  quota-limited API key.**
- **Storage is volatile and unauthenticated at rest** inside the
  container (`/tmp/app-jobs`). Jobs are evicted after 1 hour; anything
  in the container dies with it.
- CORS defaults to `*` (a deliberate demo choice — the API is
  token-gated per job, carries no cookies, and the Space is public).
- Abuse limits: request rate limit, per-request/total upload caps,
  concurrent-upload and concurrent-job caps, ZIP-bomb guards.

### `institutional` (behind your SSO / reverse proxy)

What it asserts: the app runs **behind an authenticating reverse
proxy**; the app itself still has no user accounts (job ownership,
quotas and durable persistence are roadmap items — see
`docs/audit/PLAN-REMEDIATION-2026-07-15.md`, Vague 5).

Enforced by the app in this profile:

- Startup **fails** if `CORS_ORIGINS` is the wildcard default — an
  explicit origin allowlist is required.

Required from the operator (not enforceable by the app):

- Authentication at the proxy; the app must not be directly reachable.
- TLS termination at the proxy.
- Repeat the upload size/concurrency limits at the proxy.
- Set `TRUSTED_PROXIES` to the proxy's address (never `*` on a
  directly-exposed deployment — it lets callers spoof
  `X-Forwarded-For` and bypass per-IP rate limits).
- Log hygiene at the proxy: the app never puts capability tokens in
  URLs, but treat access logs as sensitive anyway.

## Hardening already in place (both profiles)

- Capability tokens: stored as SHA-256 hashes, constant-time compare,
  404 (not 403) on wrong/missing token so job existence never leaks.
- Tokens never travel in URLs; signed, expiring, purpose-scoped
  credentials for header-less surfaces.
- Hardened XML parsing (no external entities), ZIP extraction budgets
  and member caps, path-traversal checks on image serving.
- Upload guard middleware: Content-Length required and bounded, lying
  Content-Length answered with the middleware's own 413.
- API responses are `Cache-Control: no-store`; secrets redacted from
  logs; uvicorn access logs at WARNING.
- Non-root container user; digest-pinned base images; single worker by
  design (in-process job store).
