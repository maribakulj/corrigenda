# Contributing

## Local dev setup

The repo is a monorepo with two Python distributions:

- **`packages/alto-core/`** — pure correction pipeline, published to PyPI
- **`backend/`** — FastAPI app that consumes alto-core

Install both in editable mode from the repo root:

```bash
# alto-core is a sibling package; install it first so backend's imports resolve.
pip install -e packages/alto-core

# Then the backend itself.
pip install -r backend/requirements.txt -r backend/requirements-dev.txt

# Optional: pre-commit hooks (ruff, mypy, end-of-file-fixer, ...).
pre-commit install
```

> **Why two commands?**
> Earlier the backend's `requirements.txt` listed
> `-e ../packages/alto-core` and relied on the cwd being `backend/` when
> pip ran. That fails silently when contributors install from the root
> or from arbitrary CI paths. The two-step install above is cwd-agnostic.

## Running things

```bash
# Backend tests + coverage gate
cd backend && pytest --cov

# alto-core smoke tests
cd packages/alto-core && pytest tests/

# Linters / type-checker (run from the relevant package root)
ruff check . && ruff format --check .
mypy --explicit-package-bases app    # from backend/

# Backend dev server
cd backend && uvicorn app.main:app --reload --port 8000

# Frontend dev server
cd frontend && npm install && npm run dev
```

## Docker

`docker-compose.yml` builds with `context: .` (repo root) so the
backend Dockerfile can reach `packages/alto-core/`:

```bash
docker-compose up           # backend on :8000, frontend on :5173
docker build -t alto .      # single-image HF Spaces build (port 7860)
```

## Branch + CI

- All PRs must pass: `alto-core-lint`, `alto-core-tests`, `alto-core-build`,
  `backend-lint`, `backend-types`, `backend-tests`, `backend-security`,
  `frontend`. `backend-types` and `backend-tests` block on
  `alto-core-tests` (a broken core can't sneak through).
- Coverage gate: 80% combined (`app` + `alto_core`).
- Security gate: bandit (with documented skips for B101/B108/B110) +
  `pip-audit --strict` on the resolved env.

## License

Apache 2.0 (see `LICENSE`).
