# Contributing

## Repo layout

One Python distribution and two applications around it:

- **`packages/corrigenda/`** — the correction library, the only
  *packaged* Python distribution (hatchling; PyPI publication is
  prepared by `.github/workflows/publish-corrigenda.yml` but has not
  happened yet — no tag, no release).
- **`backend/`** — FastAPI app, imported as a flat `app` package via
  `PYTHONPATH` (deliberately **not** a built package yet; see the note
  in `backend/pyproject.toml`).
- **`frontend/`** — React + TypeScript + Vite (`corrigenda-frontend`,
  private).

## Local dev setup

```bash
# corrigenda is a sibling package; install it first so backend's imports resolve.
pip install -e packages/corrigenda

# Then the backend itself.
pip install -r backend/requirements.txt -r backend/requirements-dev.txt

# Optional: pre-commit hooks (ruff, mypy, end-of-file-fixer, ...).
pre-commit install
```

> **Why two commands?**
> Earlier the backend's `requirements.txt` listed
> `-e ../packages/corrigenda` and relied on the cwd being `backend/` when
> pip ran. That fails silently when contributors install from the root
> or from arbitrary CI paths. The two-step install above is cwd-agnostic.

## Running things

```bash
# Library tests (coverage gate: 85%)
cd packages/corrigenda && pytest

# Backend tests (coverage gate: 80% on `app`; e2e run separately)
cd backend && pytest -m "not e2e" --cov
cd backend && pytest tests/e2e          # real uvicorn + fake provider

# Linters / type-checker (run from the relevant package root)
ruff check . && ruff format --check .
mypy --strict src/corrigenda            # from packages/corrigenda/
mypy --explicit-package-bases app       # from backend/

# Backend dev server
cd backend && uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend && npm install && npm run dev   # tests: npx vitest run

# Regenerate the OpenAPI snapshot + generated TS types (CI fails on drift)
scripts/generate-frontend-api-types.sh
```

## Docker

`docker-compose.yml` builds with `context: .` (repo root) so the
backend Dockerfile can reach `packages/corrigenda/`:

```bash
docker-compose up                   # backend on :8000, frontend on :5173
docker build -t corrigenda .        # single-image HF Spaces build (port 7860)
```

## CI gates (all must pass)

Python 3.11 / 3.12 / 3.13 where applicable:

- `corrigenda-lint`, `corrigenda-types` (mypy --strict),
  `corrigenda-tests` (coverage ≥ 85%), `corrigenda-build`
- `backend-lint`, `backend-types`, `backend-tests` (coverage ≥ 80% on
  `app` — the library carries its own separate gate), `backend-e2e`
  (real uvicorn server + deliberately sabotaged fake provider),
  `backend-security` (bandit + pip-audit)
- `frontend` (lint, typecheck, vitest, build, npm audit),
  `frontend-api-types-drift` (regenerates the OpenAPI snapshot +
  generated types and fails on any diff)
- `docker-build` (all three images built; the root image is
  smoke-tested: `/health`, the real SPA at `/`, a built asset,
  `/health/ready`)

`backend-types` and `backend-tests` block on `corrigenda-tests` — a
broken core can't sneak through.

## Documentation rules

Normative docs are the ones listed in the README's documentation map
(README, `SPECS_LIB_V2.md`, `packages/corrigenda/docs/`, `docs/API.md`,
`SECURITY.md`, this file). Everything under `docs/history/` is frozen
design/audit history — never update it to match the code; write the
current truth in a normative doc instead. Audit-trail references
(`Audit-Fxx`, wave numbers) belong in PRs and issues, not in new code
comments.

## License

Apache 2.0 (see `LICENSE`).
